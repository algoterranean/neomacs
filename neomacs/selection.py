"""Selections, multiple cursors and the jump list (ENGINE 5.7)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from neomacs.stability import provisional
from neomacs.text.marker import Cursor, Marker

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

JUMP_LIMIT = 100


@provisional("0.1")
class Selection:
    """An anchor and a head.  The cursor is drawn at the head; when the two
    differ, the text between them is selected."""

    __slots__ = ("_set", "anchor", "goal_x", "head")

    def __init__(self, owner: SelectionSet, head: int, anchor: int | None = None) -> None:
        self._set = owner
        self.head: Cursor = owner.buffer.cursor(head)
        # Both ends advance past text inserted exactly at them, so an empty
        # selection stays empty when something else inserts where it is.
        self.anchor: Marker = owner.buffer.marker(head if anchor is None else anchor, advance=True)
        self.goal_x: float | None = None

    # -- what is selected -----------------------------------------------------

    @property
    def buffer(self) -> Buffer:
        return self._set.buffer

    @property
    @provisional("0.1")
    def start(self) -> int:
        return min(self.anchor.pos, self.head.pos)

    @property
    @provisional("0.1")
    def end(self) -> int:
        return max(self.anchor.pos, self.head.pos)

    @property
    @provisional("0.1")
    def empty(self) -> bool:
        return self.anchor.pos == self.head.pos

    @property
    @provisional("0.1")
    def text(self) -> str:
        return self.buffer[self.start : self.end]

    @property
    @provisional("0.1")
    def pos(self) -> int:
        """Where the cursor is."""
        return self.head.pos

    # -- moving ---------------------------------------------------------------

    def _moved(self) -> None:
        self.goal_x = None
        if not self._set.extending:
            self.anchor.pos = self.head.pos

    @provisional("0.1")
    def move_to(self, pos: int) -> None:
        """Move the head.  The anchor follows unless the set is extending."""
        self.head.pos = pos
        self._moved()

    @provisional("0.1")
    def forward_char(self, n: int = 1) -> None:
        self.head.forward_char(n)
        self._moved()

    @provisional("0.1")
    def backward_char(self, n: int = 1) -> None:
        self.head.backward_char(n)
        self._moved()

    @provisional("0.1")
    def forward_word(self, n: int = 1) -> None:
        if n < 0:
            self.backward_word(-n)
            return
        syntax = self.buffer.syntax
        pos = self.head.pos
        for _ in range(n):
            pos = syntax.next_word_end(pos)
        self.move_to(pos)

    @provisional("0.1")
    def backward_word(self, n: int = 1) -> None:
        if n < 0:
            self.forward_word(-n)
            return
        syntax = self.buffer.syntax
        pos = self.head.pos
        for _ in range(n):
            pos = syntax.prev_word_start(pos)
        self.move_to(pos)

    @provisional("0.1")
    def start_of_line(self) -> None:
        self.head.start_of_line()
        self._moved()

    @provisional("0.1")
    def end_of_line(self) -> None:
        self.head.end_of_line()
        self._moved()

    @provisional("0.1")
    def forward_line(self, n: int = 1) -> None:
        """Move N logical lines."""
        self.head.forward_line(n)
        self._moved()

    @provisional("0.1")
    def select(self, anchor: int, head: int) -> None:
        """Select from ANCHOR to HEAD regardless of whether the set is extending."""
        self.anchor.pos = anchor
        self.head.pos = head
        self.goal_x = None

    @provisional("0.1")
    def collapse(self) -> None:
        """Make the selection empty, at the head."""
        self.anchor.pos = self.head.pos

    # -- editing --------------------------------------------------------------

    @provisional("0.1")
    def replace(self, text: str) -> None:
        """Replace the selected text (possibly none) with TEXT; the cursor ends after it."""
        start, end = self.start, self.end
        self.buffer.replace(start, end, text)
        self.head.pos = self.anchor.pos = start + len(text)
        self.goal_x = None
        self._set.extending = False

    @provisional("0.1")
    def insert(self, text: str) -> None:
        self.replace(text)

    @provisional("0.1")
    def delete(self) -> str:
        """Delete the selected text and return it."""
        removed = self.text
        self.replace("")
        return removed

    def detach(self) -> None:
        self.head.detach()
        self.anchor.detach()

    def __repr__(self) -> str:
        return f"<Selection {self.anchor.pos}..{self.head.pos}>"


@provisional("0.1")
class SelectionSet:
    """A view's selections.  There is always at least one."""

    def __init__(self, buffer: Buffer, pos: int = 0) -> None:
        self.buffer = buffer
        self.extending = False
        self._items: list[Selection] = [Selection(self, pos)]
        self._primary: Selection = self._items[0]
        self._last: list[tuple[int, int]] = []

    @property
    @provisional("0.1")
    def primary(self) -> Selection:
        return self._primary

    @primary.setter
    def primary(self, selection: Selection) -> None:
        if selection in self._items:
            self._primary = selection

    def __iter__(self) -> Iterator[Selection]:
        """The selections in buffer order."""
        return iter(sorted(self._items, key=lambda s: (s.start, s.end)))

    def __len__(self) -> int:
        return len(self._items)

    @provisional("0.1")
    def add(self, head: int, anchor: int | None = None) -> Selection:
        """Add a selection.  It becomes the primary one."""
        selection = Selection(self, head, anchor)
        self._items.append(selection)
        self._primary = selection
        return selection

    @provisional("0.1")
    def remove(self, selection: Selection) -> None:
        if selection not in self._items or len(self._items) == 1:
            return
        self._items.remove(selection)
        selection.detach()
        if self._primary is selection:
            self._primary = self._items[-1]

    @provisional("0.1")
    def set(self, ranges: list[tuple[int, int]], primary: int = 0) -> None:
        """Replace every selection with RANGES, given as (anchor, head) pairs."""
        if not ranges:
            return
        for old in self._items:
            old.detach()
        self._items = [Selection(self, head, anchor) for anchor, head in ranges]
        self._primary = self._items[max(0, min(primary, len(self._items) - 1))]

    @provisional("0.1")
    def keep_primary(self) -> None:
        """Remove every selection but the primary one."""
        self._remember()
        for other in self._items:
            if other is not self._primary:
                other.detach()
        self._items = [self._primary]

    @provisional("0.1")
    def collapse(self) -> None:
        """Make every selection empty."""
        self._remember()
        for selection in self._items:
            selection.collapse()
        self.extending = False

    def _remember(self) -> None:
        if len(self._items) > 1 or not self._primary.empty:
            self._last = self.ranges()

    @provisional("0.1")
    def restore_last(self) -> None:
        """Bring back the selections as they were before they last collapsed."""
        if self._last:
            current = self.ranges()
            self.set(self._last)
            self._last = current

    @provisional("0.1")
    def ranges(self) -> list[tuple[int, int]]:
        """Every selection as (anchor, head), in buffer order."""
        return [(s.anchor.pos, s.head.pos) for s in self]

    @property
    def primary_index(self) -> int:
        return list(self).index(self._primary)

    @provisional("0.1")
    def normalize(self) -> None:
        """Snap ends to grapheme cluster boundaries, then merge selections that
        overlap and empty ones at the same position (ENGINE 5.7, 5.8)."""
        buf = self.buffer
        for s in self._items:
            if s.empty:
                if not buf.is_boundary(s.head.pos):
                    s.head.pos = s.anchor.pos = buf.prev_boundary(s.head.pos)
                continue
            # A partly selected cluster becomes wholly selected.
            low, high = (s.anchor, s.head) if s.anchor.pos <= s.head.pos else (s.head, s.anchor)
            if not buf.is_boundary(low.pos):
                low.pos = buf.prev_boundary(low.pos)
            if not buf.is_boundary(high.pos):
                high.pos = buf.next_boundary(high.pos)
        if len(self._items) == 1:
            return
        ordered = sorted(self._items, key=lambda s: (s.start, s.end))
        kept: list[Selection] = [ordered[0]]
        for s in ordered[1:]:
            last = kept[-1]
            overlap = s.start < last.end or (s.start == last.end and (s.empty or last.empty))
            if not overlap:
                kept.append(s)
                continue
            # Merge S into LAST, keeping the direction of whichever is primary.
            start, end = min(last.start, s.start), max(last.end, s.end)
            keeper = s if s is self._primary else last
            backward = keeper.head.pos < keeper.anchor.pos
            keeper.anchor.pos, keeper.head.pos = (end, start) if backward else (start, end)
            loser = last if keeper is s else s
            loser.detach()
            kept[-1] = keeper
        self._items = kept
        if self._primary not in kept:
            self._primary = kept[-1]

    def detach(self) -> None:
        for selection in self._items:
            selection.detach()


@provisional("0.1")
class JumpList:
    """A bounded history of positions, pushed before long moves."""

    def __init__(self) -> None:
        self._entries: list[Marker] = []
        self._index = 0  # where `back` would go from: len means "at the present"

    @provisional("0.1")
    def push(self, buffer: Buffer, pos: int) -> None:
        """Remember POS in BUFFER as somewhere to come back to."""
        del self._entries[self._index :]
        if self._entries:
            last = self._entries[-1]
            if last.buffer is buffer and buffer.line_of(last.pos) == buffer.line_of(pos):
                last.pos = pos
                self._index = len(self._entries)
                return
        self._entries.append(buffer.marker(pos))
        if len(self._entries) > JUMP_LIMIT:
            self._entries.pop(0).detach()
        self._index = len(self._entries)

    @provisional("0.1")
    def back(self, buffer: Buffer, pos: int) -> Marker | None:
        """The previous entry, remembering the present position so `forward` can return."""
        if self._index == 0:
            return None
        if self._index == len(self._entries):
            self._entries.append(buffer.marker(pos))
        self._index -= 1
        return self._entries[self._index]

    @provisional("0.1")
    def forward(self) -> Marker | None:
        if self._index >= len(self._entries) - 1:
            return None
        self._index += 1
        return self._entries[self._index]

    def __len__(self) -> int:
        return len(self._entries)
