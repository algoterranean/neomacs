"""The scanner: strings, comments and brackets from the syntax table alone.

It walks the text keeping track of whether it is in code, a string or a
comment.  The state at the start of each line is cached and discarded from
the first changed line on, so a question usually costs a scan of one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from neomacs.syntax.table import Syntax

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer, Change

# What the scanner is inside at some point: ("code", ""), ("string", closing
# delimiter) or ("comment", closing delimiter; empty for a line comment).
State = tuple[str, str]
CODE: State = ("code", "")

SEARCH_LINES = 5000  # how far a search for a matching bracket goes
RESCAN_LINES = 20000  # how far past an edit the states of lines are compared at once


@dataclass(slots=True)
class LineScan:
    segments: list[tuple[int, int, str]]  # (start, end, "string" or "comment") within the line
    brackets: list[tuple[int, str]]  # (offset, character), in code only
    end: State


def scan_line(text: str, state: State, table: Syntax) -> LineScan:
    """Scan one line (without its newline) starting in STATE."""
    segments: list[tuple[int, int, str]] = []
    brackets: list[tuple[int, str]] = []
    opens = {o for o, _ in table.brackets}
    closes = {c for _, c in table.brackets}
    i, n = 0, len(text)
    kind, closer = state
    seg_start = 0
    while i < n:
        if kind == "string":
            if table.escape and text.startswith(table.escape, i):
                i += len(table.escape) + 1
                continue
            if text.startswith(closer, i):
                i += len(closer)
                segments.append((seg_start, i, "string"))
                kind, closer = "code", ""
                continue
            i += 1
        elif kind == "comment":
            if closer and text.startswith(closer, i):
                i += len(closer)
                segments.append((seg_start, i, "comment"))
                kind, closer = "code", ""
                continue
            i += 1
        else:
            if table.line_comment and text.startswith(table.line_comment, i):
                segments.append((i, n, "comment"))
                return LineScan(segments, brackets, CODE)
            if table.block_comment and text.startswith(table.block_comment[0], i):
                kind, closer, seg_start = "comment", table.block_comment[1], i
                i += len(table.block_comment[0])
                continue
            quote = next((q for q in table.quotes if text.startswith(q, i)), None)
            if quote is not None:
                kind, closer, seg_start = "string", quote, i
                i += len(quote)
                continue
            ch = text[i]
            if ch in opens or ch in closes:
                brackets.append((i, ch))
            i += 1
    if kind != "code":
        segments.append((seg_start, n, kind))
        # A string with a one-character delimiter does not run past its line.
        if kind == "string" and len(closer) == 1:
            return LineScan(segments, brackets, CODE)
    return LineScan(segments, brackets, (kind, closer))


class Scanner:
    def __init__(self, buffer: Buffer, table: Syntax) -> None:
        self.buffer = buffer
        self.table = table
        self._states: list[State] = [CODE]  # _states[i] is the state at the start of line i + 1
        self._lines: dict[int, tuple[int, LineScan]] = {}
        buffer.add_listener(self._changed)

    def _changed(self, change: Change) -> None:
        if not change.text_changed:
            return
        self._lines.clear()
        first = change.line  # the first line whose text changed
        # The first line the change did not touch, as it was numbered and as
        # it is now.  A change of whole lines leaves the line it ends on alone.
        whole = 0 if change.whole_lines else 1
        old_after = first + change.old_newlines + whole
        new_after = first + change.new_newlines + whole
        before = self._states[old_after - 1] if old_after <= len(self._states) else None
        old_tail = self._states[old_after - 1 :]
        del self._states[max(1, first - 1 + whole) :]
        if before is None or new_after > self.buffer.line_count:
            return
        # Did the edit change what the following lines are inside (a string
        # opened or closed, say)?  If so they look different though their text
        # is the same, and whoever shows them has to be told.
        last = None
        for i in range(min(len(old_tail), RESCAN_LINES)):
            number = new_after + i
            if number > self.buffer.line_count:
                break
            if self.state_at(number) == old_tail[i]:
                break
            last = number
        if last is not None:
            through = last
            self.buffer.after_notify(lambda: self.buffer.invalidate_lines(new_after, through))

    def state_at(self, line: int) -> State:
        """The state at the start of 1-based LINE."""
        buf = self.buffer
        while len(self._states) < line:
            number = len(self._states)
            self._states.append(scan_line(buf.line_text(number), self._states[-1], self.table).end)
        return self._states[line - 1]

    def line(self, line: int) -> LineScan:
        cached = self._lines.get(line)
        if cached is not None and cached[0] == self.buffer.tick:
            return cached[1]
        scan = scan_line(self.buffer.line_text(line), self.state_at(line), self.table)
        if len(self._lines) > 512:
            self._lines.clear()
        self._lines[line] = (self.buffer.tick, scan)
        return scan

    def kind_at(self, pos: int) -> str:
        line, offset = self.buffer.line_offset(pos)
        for start, end, kind in self.line(line).segments:
            if start <= offset < end:
                return kind
        scan = self.line(line)
        # At the very end of a line that ends inside a string or comment.
        if scan.segments and offset >= scan.segments[-1][1] == len(self.buffer.line_text(line)):
            if scan.end != CODE:
                return scan.end[0]
        return "code"

    def matching_bracket(self, pos: int) -> int | None:
        """The position of the bracket matching the one at POS, if there is one."""
        buf = self.buffer
        if not 0 <= pos < len(buf):
            return None
        ch = buf[pos]
        pair = next(((o, c) for o, c in self.table.brackets if ch in (o, c)), None)
        if pair is None or self.kind_at(pos) != "code":
            return None
        opener, closer = pair
        line, offset = buf.line_offset(pos)
        depth = 0
        if ch == opener:
            for number in range(line, min(buf.line_count, line + SEARCH_LINES) + 1):
                for at, bracket in self.line(number).brackets:
                    if number == line and at < offset:
                        continue
                    if bracket == opener:
                        depth += 1
                    elif bracket == closer:
                        depth -= 1
                        if depth == 0:
                            return buf.line_start(number) + at
        else:
            for number in range(line, max(1, line - SEARCH_LINES) - 1, -1):
                for at, bracket in reversed(self.line(number).brackets):
                    if number == line and at > offset:
                        continue
                    if bracket == closer:
                        depth += 1
                    elif bracket == opener:
                        depth -= 1
                        if depth == 0:
                            return buf.line_start(number) + at
        return None

    def enclosing_brackets(self, start: int, end: int) -> tuple[int, int] | None:
        """The innermost bracket pair that strictly contains [START, END)."""
        buf = self.buffer
        line, offset = buf.line_offset(start)
        closes = {c: o for o, c in self.table.brackets}
        opens = {o for o, _ in self.table.brackets}
        pending: list[str] = []  # closing brackets seen, awaiting their openers
        for number in range(line, max(1, line - SEARCH_LINES) - 1, -1):
            for at, bracket in reversed(self.line(number).brackets):
                if number == line and at >= offset:
                    continue
                if bracket in closes:
                    pending.append(bracket)
                elif bracket in opens:
                    if pending:
                        pending.pop()
                        continue
                    open_pos = buf.line_start(number) + at
                    close_pos = self.matching_bracket(open_pos)
                    if close_pos is not None and close_pos >= end:
                        return open_pos, close_pos + 1
                    return None
        return None
