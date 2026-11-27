"""The parse-tree layer (ENGINE 5.9): tree-sitter, where it can be loaded.

Everything that touches the binding is in this module, and so is every
conversion between the editor's positions (code points) and tree-sitter's
(UTF-8 bytes).  Nothing here is required: a buffer with no tree is served by
the scanner.
"""

from __future__ import annotations

import importlib
import sys
import sysconfig
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs.options import Option

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer, Change

Option(
    "tree_sitter",
    str,
    "auto",
    "Whether to load the tree-sitter parser: auto, always or never.  On a "
    "free-threaded Python, auto does not load it if doing so would turn the "
    "global interpreter lock back on.",
    validate=lambda v: v in ("auto", "always", "never"),
)

QUERIES = Path(__file__).with_name("queries")
BACKGROUND_SIZE = (
    200_000  # characters: a first parse of more than this is done off the owner thread
)
CHUNK = 8192  # characters handed to the parser at a time

_languages: dict[str, Any] = {}
_failures: dict[str, str] = {}
_queries: dict[tuple[str, str], Any] = {}


def why_not(policy: str) -> str | None:
    """Why the binding may not be loaded under POLICY, or None if it may."""
    if policy == "never":
        return "the option tree_sitter is 'never'"
    if policy == "always" or "tree_sitter" in sys.modules:
        return None
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    is_enabled: Any = getattr(sys, "_is_gil_enabled", lambda: True)
    if free_threaded and not is_enabled() and getattr(sys.flags, "gil", None) != 0:
        # Importing an extension that has not declared itself safe turns the
        # lock on for the whole process.  Find out without importing it.
        return (
            "loading it would turn the global interpreter lock back on; set the "
            "option tree_sitter to 'always' to load it anyway, or start with PYTHON_GIL=0"
        )
    return None


def language(grammar: str, policy: str = "auto") -> Any:
    """The tree-sitter language for GRAMMAR, or None; `failure(grammar)` says why."""
    if policy == "never":
        return None  # whatever another buffer has loaded
    if grammar in _languages:
        return _languages[grammar]
    reason = why_not(policy)
    if reason is None:
        try:
            binding = importlib.import_module("tree_sitter")
            module = importlib.import_module(f"tree_sitter_{grammar.replace('-', '_')}")
            _languages[grammar] = binding.Language(module.language())
            _failures.pop(grammar, None)
            return _languages[grammar]
        except Exception as exc:  # not installed, or built for another Python
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    _failures[grammar] = reason
    return None


def failure(grammar: str) -> str:
    return _failures.get(grammar, "")


def query(grammar: str, kind: str) -> Any:
    """The compiled query of KIND ("highlights", "textobjects") for GRAMMAR, or None."""
    key = (grammar, kind)
    if key not in _queries:
        found = None
        path = QUERIES / grammar / f"{kind}.scm"
        lang = _languages.get(grammar)
        if lang is not None and path.exists():
            binding = importlib.import_module("tree_sitter")
            try:
                found = binding.Query(lang, path.read_text(encoding="utf-8"))
            except Exception as exc:
                # A query written for another version of the grammar.  Do
                # without it rather than fail every command that asks.
                _failures[f"{grammar}/{kind}"] = f"{type(exc).__name__}: {exc}"
        _queries[key] = found
    return _queries[key]


class BufferTree:
    """One buffer's parse tree, kept in step with its text."""

    def __init__(self, buffer: Buffer, grammar: str, lang: Any) -> None:
        binding = importlib.import_module("tree_sitter")
        self.buffer = buffer
        self.grammar = grammar
        self.language = lang
        self._binding = binding
        self._parser = binding.Parser(lang)
        self._tree: Any = None
        self._stale = True  # the text has changed since the tree was parsed
        self._edited: tuple[int, int] | None = None  # lines edited since the last parse
        self._background: threading.Thread | None = None
        self._since_snapshot: list[dict[str, Any]] = []
        self.generation = 0  # goes up with every parse
        self.parses = 0
        self.background_parses = 0
        buffer.add_listener(self._changed)
        # Reparse before a view works out what to send, so that lines whose
        # structure changed go out in the same update as the edit.
        buffer.display_preparers.append(self._prepare)
        if len(buffer) > BACKGROUND_SIZE:
            self._start_background()

    def close(self) -> None:
        self.buffer.remove_listener(self._changed)
        if self._prepare in self.buffer.display_preparers:
            self.buffer.display_preparers.remove(self._prepare)

    def _prepare(self) -> None:
        if self._stale:
            self.ensure()

    # -- positions --------------------------------------------------------------

    def byte(self, pos: int) -> int:
        return self.buffer.store.byte_of(pos)

    def pos(self, byte: int) -> int:
        return self.buffer.store.pos_of_byte(min(byte, self.buffer.store.byte_of(len(self.buffer))))

    def _point(self, pos: int) -> tuple[int, int]:
        buffer = self.buffer
        line = buffer.line_of(pos)
        return line - 1, self.byte(pos) - self.byte(buffer.line_start(line))

    def span(self, node: Any) -> tuple[int, int]:
        """A node's extent as editor positions."""
        return self.pos(node.start_byte), self.pos(node.end_byte)

    # -- keeping in step ----------------------------------------------------------

    def _changed(self, change: Change) -> None:
        if not change.text_changed:
            return
        removed = change.removed
        start_byte = self.byte(change.start)
        row, column = self._point(change.start)
        removed_bytes = len(removed.encode("utf-8"))
        newlines = removed.count("\n")
        if newlines:
            tail = len(removed.rsplit("\n", 1)[1].encode("utf-8"))
            old_end = (row + newlines, tail)
        else:
            old_end = (row, column + removed_bytes)
        edit = {
            "start_byte": start_byte,
            "old_end_byte": start_byte + removed_bytes,
            "new_end_byte": self.byte(change.new_end),
            "start_point": (row, column),
            "old_end_point": old_end,
            "new_end_point": self._point(change.new_end),
        }
        if self._background is not None:
            self._since_snapshot.append(edit)
        if self._tree is not None:
            self._tree.edit(**edit)
        self._stale = True
        first, last = change.line, change.line + change.new_newlines
        if self._edited is not None:
            first, last = min(first, self._edited[0]), max(last, self._edited[1])
        self._edited = (first, last)

    def _read(self, byte: int, _point: Any) -> bytes:
        store = self.buffer.store
        if byte >= store.byte_of(len(store)):
            return b""
        start = store.pos_of_byte(byte)
        return store.slice(start, min(len(store), start + CHUNK)).encode("utf-8")

    def _start_background(self) -> None:
        """Parse a snapshot on another thread; the scanner answers meanwhile."""
        from neomacs.editor import current

        try:
            editor = current()
        except LookupError:
            return  # no editor running: the first query will parse in place
        if editor.loop is None:
            return
        data = self.buffer.text.encode("utf-8")
        parser = self._binding.Parser(self.language)
        self._since_snapshot = []

        def work() -> None:
            tree = parser.parse(data)
            editor.call_from_thread(self._adopt, tree)

        self._background = threading.Thread(target=work, name="neomacs-first-parse", daemon=True)
        self._background.start()

    def _adopt(self, tree: Any) -> None:
        """On the owner thread: take the tree the worker made, brought up to date."""
        self._background = None
        self.background_parses += 1
        for edit in self._since_snapshot:
            tree.edit(**edit)
        stale = bool(self._since_snapshot)
        self._since_snapshot = []
        self._tree, self._stale = tree, stale
        self._edited = None
        self.generation += 1
        if stale:
            self.ensure()
        self.buffer.invalidate(0, len(self.buffer))  # everything can be shown properly now

    def ensure(self) -> Any:
        """The tree, reparsed if the text has changed.  None while the first parse is running."""
        if self._background is not None:
            return None
        if self._tree is not None and not self._stale:
            return self._tree
        old = self._tree
        new = (
            self._parser.parse(self._read, old)
            if old is not None
            else self._parser.parse(self._read)
        )
        self.parses += 1
        self.generation += 1
        self._edited = None
        self._tree, self._stale = new, False
        if old is not None and new is not None:
            # Structure may have changed beyond the lines that were edited, and
            # those lines look different now though their text is the same.
            for changed in old.changed_ranges(new):
                first, last = changed.start_point[0] + 1, changed.end_point[0] + 1
                self.buffer.after_notify(lambda a=first, b=last: self.buffer.invalidate_lines(a, b))
            if not self.buffer.in_notify:
                self.buffer.run_after_notify()
        return new

    # -- looking things up ----------------------------------------------------------

    def node_at(self, pos: int, *, named: bool = False) -> Any:
        """The smallest node containing the character at POS."""
        tree = self.ensure()
        if tree is None:
            return None
        byte = self.byte(pos)
        end = self.byte(min(len(self.buffer), pos + 1))
        root = tree.root_node
        if named:
            return root.named_descendant_for_byte_range(byte, max(byte, end))
        return root.descendant_for_byte_range(byte, max(byte, end))

    def broken_near(self, start: int, end: int) -> bool:
        """Whether the code around [START, END) did not parse, so that an
        answer from the tree there cannot be trusted."""
        tree = self.ensure()
        if tree is None:
            return True
        root = tree.root_node
        if not root.has_error:
            return False
        buffer = self.buffer
        a = self.byte(buffer.line_start(buffer.line_of(start)))
        b = self.byte(buffer.line_end(buffer.line_of(end)))
        node = root.descendant_for_byte_range(a, b)
        while node is not None and node.parent is not None and node.parent.parent is not None:
            node = node.parent  # up to the top-level statement
        return node is None or node.has_error or node.is_missing or node is root

    def captures(self, kind: str, start: int, end: int) -> list[tuple[int, int, str, int]]:
        """(start, end, capture name, pattern number) for KIND's query within [START, END)."""
        tree = self.ensure()
        compiled = query(self.grammar, kind)
        if tree is None or compiled is None:
            return []
        cursor = self._binding.QueryCursor(compiled)
        cursor.set_byte_range(self.byte(start), self.byte(end))
        found: list[tuple[int, int, str, int]] = []
        for pattern, captured in cursor.matches(tree.root_node):
            for name, nodes in captured.items():
                for node in nodes:
                    a, b = self.span(node)
                    if a < end and b > start:
                        found.append((a, b, name, pattern))
        return found
