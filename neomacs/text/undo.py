"""Undo (ENGINE 5.5): a linear log with a redo stack."""

from __future__ import annotations

import contextlib
import contextvars
import itertools
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

# The command invocation in progress, if any.  Changes made under one
# invocation form one undo group per buffer.
invocation: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "neomacs_invocation", default=None
)
_ids = itertools.count(1)


def new_invocation() -> int:
    return next(_ids)


@dataclass
class Edit:
    pos: int
    removed: str
    inserted: str


@dataclass
class UndoGroup:
    invocation: int
    edits: list[Edit] = field(default_factory=list[Edit])
    # Whatever the view layer wants restored: selections before and after.
    before: Any = None
    after: Any = None


@provisional("0.1")
class UndoLog:
    def __init__(self, buffer: Buffer) -> None:
        self.buffer = buffer
        self._undo: list[UndoGroup] = []
        self._redo: list[UndoGroup] = []
        self._replaying = False

    def record(self, pos: int, removed: str, inserted: str) -> None:
        """Called by the buffer for every change."""
        if self._replaying:
            return
        self._redo.clear()
        inv = invocation.get()
        if inv is not None and self._undo and self._undo[-1].invocation == inv:
            group = self._undo[-1]
        else:
            group = UndoGroup(inv if inv is not None else new_invocation())
            self._undo.append(group)
        group.edits.append(Edit(pos, removed, inserted))

    @contextlib.contextmanager
    @provisional("0.1")
    def group(self) -> Generator[None]:
        """Make every change in the block one undo step."""
        if invocation.get() is not None:
            yield
            return
        token = invocation.set(new_invocation())
        try:
            yield
        finally:
            invocation.reset(token)

    @property
    @provisional("0.1")
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    @provisional("0.1")
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def last(self) -> UndoGroup | None:
        """The most recent group, for attaching selections to."""
        return self._undo[-1] if self._undo else None

    @provisional("0.1")
    def undo(self) -> UndoGroup | None:
        """Undo the most recent group.  Returns it, or None if there is nothing to undo."""
        if not self._undo:
            return None
        group = self._undo.pop()
        self._replaying = True
        try:
            for edit in reversed(group.edits):
                self.buffer.replace(edit.pos, edit.pos + len(edit.inserted), edit.removed)
        finally:
            self._replaying = False
        self._redo.append(group)
        return group

    @provisional("0.1")
    def redo(self) -> UndoGroup | None:
        if not self._redo:
            return None
        group = self._redo.pop()
        self._replaying = True
        try:
            for edit in group.edits:
                self.buffer.replace(edit.pos, edit.pos + len(edit.removed), edit.inserted)
        finally:
            self._replaying = False
        self._undo.append(group)
        return group

    @provisional("0.1")
    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
