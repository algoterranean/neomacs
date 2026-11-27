"""Markers and cursors (ENGINE 5.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer


@provisional("0.1")
class Marker:
    """A position that moves with the text.

    ADVANCE says which way it goes when text is inserted exactly at it: a
    marker that advances ends up after the new text.
    """

    __slots__ = ("__weakref__", "_pos", "advance", "buffer")

    def __init__(self, buffer: Buffer, pos: int = 0, *, advance: bool = False) -> None:
        self.buffer = buffer
        self._pos = buffer.clamp(pos)
        self.advance = advance
        buffer._markers.add(self)  # pyright: ignore[reportPrivateUsage]

    @property
    @provisional("0.1")
    def pos(self) -> int:
        return self._pos

    @pos.setter
    def pos(self, value: int) -> None:
        self._pos = self.buffer.clamp(value)

    def __int__(self) -> int:
        return self._pos

    def __index__(self) -> int:
        return self._pos

    @provisional("0.1")
    def detach(self) -> None:
        """Stop tracking edits."""
        self.buffer._markers.discard(self)  # pyright: ignore[reportPrivateUsage]

    def _adjust(self, start: int, end: int, inserted: int) -> None:
        """Account for the text in [START, END) having been replaced by INSERTED characters."""
        p = self._pos
        if p < start:
            return
        if p > end:
            self._pos = p + inserted - (end - start)
        elif start == end:  # pure insertion exactly here
            if self.advance:
                self._pos = p + inserted
        elif p == end:  # just after the replaced text: stay after it
            self._pos = start + inserted
        elif p == start:  # just before it: stay before
            pass
        else:  # inside what was removed
            self._pos = start + inserted if self.advance else start

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._pos} in {self.buffer.name!r}>"


@provisional("0.1")
class Cursor(Marker):
    """A marker with movement and editing methods."""

    __slots__ = ()

    def __init__(self, buffer: Buffer, pos: int = 0) -> None:
        super().__init__(buffer, pos, advance=True)

    # -- where ----------------------------------------------------------------

    @property
    @provisional("0.1")
    def line(self) -> int:
        """The 1-based line the cursor is on."""
        return self.buffer.line_of(self._pos)

    @property
    @provisional("0.1")
    def offset(self) -> int:
        """Code points from the start of the line."""
        return self._pos - self.buffer.line_start(self.line)

    @property
    @provisional("0.1")
    def column(self) -> int:
        """Grapheme clusters from the start of the line."""
        start = self.buffer.line_start(self.line)
        return sum(1 for _ in self.buffer.clusters(start, self._pos))

    # -- movement -------------------------------------------------------------

    @provisional("0.1")
    def move_to(self, pos: int) -> None:
        self.pos = pos

    @provisional("0.1")
    def forward_char(self, n: int = 1) -> None:
        """Move forward N grapheme clusters (backward if N is negative)."""
        if n < 0:
            self.backward_char(-n)
            return
        pos = self._pos
        for _ in range(n):
            pos = self.buffer.next_boundary(pos)
        self._pos = pos

    @provisional("0.1")
    def backward_char(self, n: int = 1) -> None:
        if n < 0:
            self.forward_char(-n)
            return
        pos = self._pos
        for _ in range(n):
            pos = self.buffer.prev_boundary(pos)
        self._pos = pos

    @provisional("0.1")
    def start_of_line(self) -> None:
        self._pos = self.buffer.line_start(self.line)

    @provisional("0.1")
    def end_of_line(self) -> None:
        self._pos = self.buffer.line_end(self.line)

    @provisional("0.1")
    def forward_line(self, n: int = 1) -> None:
        """Move N logical lines, keeping the offset within the line where possible."""
        buf = self.buffer
        line = self.line
        offset = self._pos - buf.line_start(line)
        target = max(1, min(buf.line_count, line + n))
        start = buf.line_start(target)
        pos = min(start + offset, buf.line_end(target))
        self._pos = pos if buf.is_boundary(pos) else buf.prev_boundary(pos)

    # -- editing --------------------------------------------------------------

    @provisional("0.1")
    def insert(self, text: str) -> None:
        """Insert TEXT at the cursor, leaving the cursor after it."""
        pos = self._pos
        self.buffer.insert(pos, text)
        self._pos = pos + len(text)

    @provisional("0.1")
    def delete_char(self, n: int = 1) -> str:
        """Delete N grapheme clusters after the cursor; return the text removed."""
        end = self._pos
        for _ in range(n):
            end = self.buffer.next_boundary(end)
        return self.buffer.delete(self._pos, end)

    @provisional("0.1")
    def delete_backward_char(self, n: int = 1) -> str:
        """Delete N grapheme clusters before the cursor; return the text removed."""
        start = self._pos
        for _ in range(n):
            start = self.buffer.prev_boundary(start)
        return self.buffer.delete(start, self._pos)
