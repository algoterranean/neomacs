"""Highlighting from the syntax table alone (ENGINE 5.4, 5.9).

A decoration provider: asked about a line, it answers with faces for the
strings and comments the scanner finds there, and for the keywords the mode
lists.  Results are cached per line and reused for as long as the line's
text and the scanner's state at its start are the same.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from neomacs.syntax import tree as tree_module
from neomacs.syntax.scan import State

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

Decoration = tuple[int, int, str, int]
_WORD = re.compile(r"[^\W\d]\w*|\d[\w.]*|@")
PRIORITY = -10  # below spans, so a search match or a selection shows over it
CACHE_LINES = 4096
TREE_PRIORITY = -1000
# The captures of a highlights query that are shown, each with the face of the same name.
TREE_FACES = frozenset(
    {"comment", "string", "number", "constant", "keyword", "function", "type", "decorator"}
)


class ScannerHighlighter:
    """Faces for one buffer's lines, from its mode's table and word lists."""

    def __init__(self, buffer: Buffer) -> None:
        self.buffer = buffer
        # line text -> {state at its start: decorations relative to the line}
        self._cache: dict[tuple[str, State], list[tuple[int, int, str]]] = {}
        self.computed = 0  # lines worked out rather than found in the cache
        self.from_tree = 0  # lines answered from a parse tree

    def __call__(self, buffer: Buffer, start: int, end: int) -> Iterable[Decoration]:
        """The provider interface: decorations for the line that is [START, END)."""
        tree = buffer.syntax.tree
        if (
            tree is not None
            and tree_module.query(tree.grammar, "highlights") is not None
            and tree.ensure() is not None
        ):
            # A parse tree knows more than the table does; a later pattern of
            # the query wins where two apply.
            self.from_tree += 1
            return [
                (max(a, start), min(b, end), name, TREE_PRIORITY + pattern)
                for a, b, name, pattern in tree.captures("highlights", start, end)
                if name in TREE_FACES
            ]
        line = buffer.line_of(start)
        text = buffer.store.slice(start, end)
        scanner = buffer.syntax.scanner
        key = (text, scanner.state_at(line))
        found = self._cache.get(key)
        if found is None:
            found = self._decorate(text, key[1], line)
            if len(self._cache) >= CACHE_LINES:
                self._cache.clear()
            self._cache[key] = found
            self.computed += 1
        return [(start + a, start + b, face, PRIORITY) for a, b, face in found]

    def _decorate(self, text: str, state: State, line: int) -> list[tuple[int, int, str]]:
        from neomacs.syntax.scan import scan_line

        buffer = self.buffer
        mode: Any = buffer.mode
        scan = scan_line(text, state, buffer.syntax.table)
        found: list[tuple[int, int, str]] = [(a, b, kind) for a, b, kind in scan.segments]
        keywords: frozenset[str] = getattr(mode, "keywords", frozenset[str]())
        constants: frozenset[str] = getattr(mode, "constants", frozenset[str]())
        definers: dict[str, str] = getattr(mode, "definers", {})
        if not (keywords or constants):
            return found
        covered = scan.segments
        expect: str | None = None  # the face for the next name, after `def` or `class`
        for match in _WORD.finditer(text):
            a, b = match.span()
            if any(s <= a < e for s, e, _ in covered):
                continue
            word = match.group()
            if word == "@":
                if not text[:a].strip():  # a decorator: the whole dotted name
                    end = a + 1
                    while end < len(text) and (text[end].isalnum() or text[end] in "._"):
                        end += 1
                    found.append((a, end, "decorator"))
                continue
            if expect is not None and not word[0].isdigit():
                found.append((a, b, expect))
                expect = None
                continue
            if word[0].isdigit():
                found.append((a, b, "number"))
            elif word in keywords:
                found.append((a, b, "keyword"))
                expect = definers.get(word)
            elif word in constants:
                found.append((a, b, "constant"))
        found.sort()
        return found


def install(buffer: Buffer) -> None:
    """Give BUFFER the scanner's highlighting."""
    uninstall(buffer)
    provider = ScannerHighlighter(buffer)
    buffer.providers.append(provider)
    buffer.locals["_highlighter"] = provider
    buffer.invalidate(0, len(buffer))


def uninstall(buffer: Buffer) -> None:
    old = buffer.locals.pop("_highlighter", None)
    if old in buffer.providers:
        buffer.providers.remove(old)
        buffer.invalidate(0, len(buffer))
