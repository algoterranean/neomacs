"""Standard commands: adding and removing cursors."""

from __future__ import annotations

from neomacs.commands import command
from neomacs.editor import ed


def _add_on_line(delta: int) -> None:
    view = ed.view
    buf = view.buffer
    ordered = list(view.selections)
    edge = ordered[-1] if delta > 0 else ordered[0]
    line, offset = buf.line_offset(edge.head.pos)
    target = line + delta
    if not 1 <= target <= buf.line_count:
        ed.message("No more lines")
        return
    pos = buf.position(target, offset)
    view.selections.add(pos if buf.is_boundary(pos) else buf.prev_boundary(pos))


@command
def add_cursor_below() -> None:
    """Add a cursor on the line below the last one, at the same offset."""
    _add_on_line(1)


@command
def add_cursor_above() -> None:
    """Add a cursor on the line above the first one, at the same offset."""
    _add_on_line(-1)


@command
def keep_primary_cursor() -> None:
    """Remove every cursor but the primary one."""
    ed.view.selections.keep_primary()
