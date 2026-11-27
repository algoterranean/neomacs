"""`buf.syntax`: one interface over the syntax table and, later, a parse tree."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from neomacs.stability import provisional
from neomacs.syntax.scan import Scanner
from neomacs.syntax.table import DEFAULT, Syntax
from neomacs.syntax.tree import BufferTree

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer


@provisional("0.1")
class BufferSyntax:
    def __init__(self, buffer: Buffer) -> None:
        self.buffer = buffer
        self._tree: BufferTree | None = None
        self._tree_looked_for = False
        self._scanner: Scanner | None = None

    @property
    @provisional("0.1")
    def tree(self) -> BufferTree | None:
        """The parse tree, if the buffer's mode names a grammar that can be loaded."""
        if not self._tree_looked_for:
            self._tree_looked_for = True
            grammar = getattr(self.buffer.mode, "grammar", None)
            if grammar:
                from neomacs.syntax import tree as tree_module

                policy = str(self.buffer.options.get("tree_sitter", "auto"))
                lang = tree_module.language(grammar, policy)
                if lang is not None:
                    self._tree = BufferTree(self.buffer, grammar, lang)
                else:
                    _note_once(
                        f"No parse tree for {grammar} ({tree_module.failure(grammar)}); "
                        "the syntax table is used instead"
                    )
        return self._tree

    def close(self) -> None:
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        if self._scanner is not None:
            self.buffer.remove_listener(self._scanner._changed)  # pyright: ignore[reportPrivateUsage]

    def _trusted_tree(self, start: int, end: int) -> BufferTree | None:
        """The tree, if there is one and the code around [START, END) parsed.
        While code is being typed it often has not, and the scanner answers."""
        tree = self.tree
        if tree is None or tree.ensure() is None or tree.broken_near(start, end):
            return None
        return tree

    @property
    @provisional("0.1")
    def table(self) -> Syntax:
        """The syntax table: the buffer's mode's, or the default."""
        table = getattr(self.buffer.mode, "syntax", None)
        return table if isinstance(table, Syntax) else DEFAULT

    @property
    def scanner(self) -> Scanner:
        table = self.table
        if self._scanner is None or self._scanner.table is not table:
            if self._scanner is not None:
                self.buffer.remove_listener(self._scanner._changed)  # pyright: ignore[reportPrivateUsage]
            self._scanner = Scanner(self.buffer, table)
        return self._scanner

    # -- words: from the table alone ----------------------------------------

    def _is_word_at(self, pos: int) -> bool:
        return pos < len(self.buffer) and self.table.is_word(self.buffer[pos])

    @provisional("0.1")
    def next_word_end(self, pos: int) -> int:
        """The end of the word at or after POS."""
        buf = self.buffer
        size = len(buf)
        while pos < size and not self._is_word_at(pos):
            pos = buf.next_boundary(pos)
        while pos < size and self._is_word_at(pos):
            pos = buf.next_boundary(pos)
        return pos

    @provisional("0.1")
    def prev_word_start(self, pos: int) -> int:
        """The start of the word at or before POS."""
        buf = self.buffer
        while pos > 0 and not self._is_word_at(buf.prev_boundary(pos)):
            pos = buf.prev_boundary(pos)
        while pos > 0 and self._is_word_at(buf.prev_boundary(pos)):
            pos = buf.prev_boundary(pos)
        return pos

    @provisional("0.1")
    def word_at(self, pos: int) -> tuple[int, int] | None:
        """The word containing or touching POS, as (start, end)."""
        buf = self.buffer
        if not self._is_word_at(pos) and not (pos > 0 and self._is_word_at(buf.prev_boundary(pos))):
            return None
        start = pos
        while start > 0 and self._is_word_at(buf.prev_boundary(start)):
            start = buf.prev_boundary(start)
        end = pos
        while end < len(buf) and self._is_word_at(end):
            end = buf.next_boundary(end)
        return start, end

    # -- structure: the scanner (a tree will take these over when there is one) --

    @provisional("0.1")
    def kind_at(self, pos: int) -> str:
        """ "code", "string" or "comment"."""
        pos = self.buffer.clamp(pos)
        tree = self._trusted_tree(pos, pos)
        if tree is not None and pos < len(self.buffer):
            node = tree.node_at(pos)
            while node is not None:
                if node.type == "comment":
                    return "comment"
                if node.type == "string" or node.type.startswith("string_"):
                    return "string"
                if node.type == "interpolation":
                    return "code"
                node = node.parent
            return "code"
        return self.scanner.kind_at(pos)

    @provisional("0.1")
    def matching_bracket(self, pos: int) -> int | None:
        tree = self._trusted_tree(pos, pos) if 0 <= pos < len(self.buffer) else None
        if tree is not None:
            pairs = dict(self.table.brackets)
            closers = {c: o for o, c in pairs.items()}
            ch = self.buffer[pos]
            node = tree.node_at(pos)
            if node is not None and node.type == ch and node.parent is not None:
                siblings = node.parent.children
                if ch in pairs:
                    partner = next((n for n in reversed(siblings) if n.type == pairs[ch]), None)
                elif ch in closers:
                    partner = next((n for n in siblings if n.type == closers[ch]), None)
                else:
                    partner = None
                if partner is not None:
                    return tree.pos(partner.start_byte)
            if ch not in pairs and ch not in closers:
                return None
        return self.scanner.matching_bracket(pos)

    @provisional("0.1")
    def enclosing(self, start: int, end: int) -> tuple[int, int] | None:
        """The next larger range around [START, END): a bracket pair, then the
        line, then the paragraph."""
        buf = self.buffer
        tree = self._trusted_tree(start, end)
        if tree is not None:
            a, b = tree.byte(start), tree.byte(end)
            root = tree.ensure().root_node
            node = root.named_descendant_for_byte_range(a, max(a, b))
            while node is not None and tree.span(node) == (start, end):
                node = node.parent
            if node is not None and node.parent is not None:
                return tree.span(node)
            return (0, len(buf)) if (start, end) != (0, len(buf)) else None
        found = self.scanner.enclosing_brackets(start, end)
        if found is not None:
            return found
        line1, line2 = buf.line_of(start), buf.line_of(end)
        whole = (buf.line_start(line1), buf.line_end(line2))
        if whole != (start, end) and whole[0] <= start and whole[1] >= end:
            return whole
        first, last = line1, line2
        while first > 1 and buf.line_text(first - 1).strip():
            first -= 1
        while last < buf.line_count and buf.line_text(last + 1).strip():
            last += 1
        paragraph = (buf.line_start(first), buf.line_end(last))
        return paragraph if paragraph != (start, end) else None

    @provisional("0.1")
    def expression_at(self, pos: int, direction: int = 1) -> tuple[int, int] | None:
        """The expression after POS (DIRECTION 1) or before it (-1): a bracketed
        group, a string, or a run of identifier characters."""
        buf = self.buffer
        size = len(buf)
        table = self.table
        scanner = self.scanner
        if direction >= 0:
            while pos < size and buf[pos].isspace():
                pos += 1
            if pos >= size:
                return None
            other = scanner.matching_bracket(pos)
            if other is not None and other > pos:
                return pos, other + 1
            if scanner.kind_at(pos) == "string":
                end = pos + 1
                while end < size and scanner.kind_at(end) == "string":
                    end += 1
                return pos, end
            end = pos
            while end < size and table.is_symbol(buf[end]):
                end += 1
            return (pos, end) if end > pos else (pos, buf.next_boundary(pos))
        while pos > 0 and buf[pos - 1].isspace():
            pos -= 1
        if pos <= 0:
            return None
        other = scanner.matching_bracket(pos - 1)
        if other is not None and other < pos - 1:
            return other, pos
        if scanner.kind_at(pos - 1) == "string":
            start = pos - 1
            while start > 0 and scanner.kind_at(start - 1) == "string":
                start -= 1
            return start, pos
        start = pos
        while start > 0 and table.is_symbol(buf[start - 1]):
            start -= 1
        return (start, pos) if start < pos else (buf.prev_boundary(pos), pos)

    @provisional("0.1")
    def indentation(self, line: int) -> int:
        """The width of LINE's leading whitespace, a tab advancing to the next tab stop."""
        width = 0
        tab = int(self.buffer.options.tab_width)
        for ch in self.buffer.line_text(line):
            if ch == " ":
                width += 1
            elif ch == "\t":
                width += tab - width % tab
            else:
                break
        return width

    @provisional("0.1")
    def indent_for(self, line: int) -> int | None:
        """A suggested indentation for LINE: the mode's, or failing that the
        previous line's, adjusted by bracket depth."""
        buf = self.buffer
        mode = buf.mode
        if mode is not None:
            mode_class = cast(Any, type(mode))
            wanted: int | None = mode_class.indent_for(buf, line)
            if wanted is not None:
                return wanted
        previous = line - 1
        while previous >= 1 and not buf.line_text(previous).strip():
            previous -= 1
        if previous < 1:
            return 0
        opens = {o for o, _ in self.table.brackets}
        closes = {c for _, c in self.table.brackets}
        unit = int(buf.options.tab_width)
        depth = 0
        for _, bracket in self.scanner.line(previous).brackets:
            depth += 1 if bracket in opens else -1
        indent = self.indentation(previous) + (unit if depth > 0 else 0)
        if buf.line_text(line).lstrip()[:1] in closes:
            indent -= unit
        return max(0, indent)

    # -- named units: need a tree ---------------------------------------------

    @provisional("0.1")
    def unit_at(self, pos: int, name: str) -> tuple[int, int] | None:
        """The innermost function, class or other named unit containing POS.
        None without a grammar, or where the code does not parse."""
        tree = self._trusted_tree(pos, pos)
        if tree is None:
            return None
        end = min(len(self.buffer), pos + 1)
        found = [
            (a, b) for a, b, capture, _ in tree.captures("textobjects", pos, end)
            if capture == name and a <= pos <= b
        ]  # fmt: skip
        if not found:
            return None
        start, stop = min(found, key=lambda r: r[1] - r[0])
        # With its decorators, when it has them.
        return min((r for r in found if r[1] == stop and r[0] <= start), key=lambda r: r[0])

    @provisional("0.1")
    def units(self, name: str, start: int, end: int) -> list[tuple[int, int]]:
        """Every unit called NAME that intersects [START, END), outermost form
        of each, in order.  Empty without a grammar."""
        tree = self.tree
        if tree is None or tree.ensure() is None:
            return []
        by_end: dict[int, int] = {}
        for a, b, capture, _ in tree.captures("textobjects", start, end):
            if capture == name:
                by_end[b] = min(a, by_end.get(b, a))
        return sorted((a, b) for b, a in by_end.items())


def _note_once(text: str) -> None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    if editor is not None:
        editor.note_once(text)
