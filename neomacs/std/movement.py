"""Standard commands: moving the cursors."""

from __future__ import annotations

from typing import Annotated

from neomacs.commands import Ask, Prefix, command
from neomacs.editor import ed
from neomacs.selection import Selection
from neomacs.text.marker import Marker


@command
def forward_char(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move forward N characters."""
    sel.forward_char(n)


@command
def backward_char(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move backward N characters."""
    sel.backward_char(n)


@command
def forward_word(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move forward N words."""
    sel.forward_word(n)


@command
def backward_word(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move backward N words."""
    sel.backward_word(n)


@command
def start_of_line(sel: Selection) -> None:
    """Move to the start of the line."""
    sel.start_of_line()


@command
def end_of_line(sel: Selection) -> None:
    """Move to the end of the line."""
    sel.end_of_line()


@command
async def next_line(n: Annotated[int, Prefix()] = 1) -> None:
    """Move down N rows as displayed."""
    await ed.view.move_visual(n)


@command
async def previous_line(n: Annotated[int, Prefix()] = 1) -> None:
    """Move up N rows as displayed."""
    await ed.view.move_visual(-n)


@command
async def page_down(n: Annotated[int, Prefix()] = 1) -> None:
    """Move down N pages."""
    await ed.view.move_visual(n, "page")


@command
async def page_up(n: Annotated[int, Prefix()] = 1) -> None:
    """Move up N pages."""
    await ed.view.move_visual(-n, "page")


def _long_move(pos: int) -> None:
    """Move the primary cursor to POS, leaving a jump-list entry behind."""
    view = ed.view
    view.jumps.push(view.buffer, view.cursor.pos)
    if not view.selections.extending:
        view.selections.keep_primary()
    view.selections.primary.move_to(pos)


@command
def buffer_start() -> None:
    """Move to the start of the buffer."""
    _long_move(0)


@command
def buffer_end() -> None:
    """Move to the end of the buffer."""
    _long_move(len(ed.buffer))


@command
def goto_line(line: Annotated[int, Ask.integer("Go to line: ")]) -> None:
    """Move to the start of LINE."""
    buffer = ed.buffer
    _long_move(buffer.line_start(max(1, min(buffer.line_count, line))))


def _go_to(marker: Marker) -> None:
    """Put the cursor at a jump-list entry, showing its buffer if it is another one."""
    view = ed.view
    if marker.buffer is not view.buffer:
        if ed.buffers.get(marker.buffer.name) is not marker.buffer:
            ed.message("That buffer is gone")
            return
        view.show(marker.buffer)
    view.selections.keep_primary()
    view.selections.primary.select(marker.pos, marker.pos)
    view.reveal()


@command
def jump_back() -> None:
    """Go back to where the last long move started, in whichever buffer that was."""
    view = ed.view
    marker = view.jumps.back(view.buffer, view.cursor.pos)
    if marker is None:
        ed.message("No earlier position")
    else:
        _go_to(marker)


@command
def jump_forward() -> None:
    """Go forward again after `jump_back`."""
    marker = ed.view.jumps.forward()
    if marker is None:
        ed.message("No later position")
    else:
        _go_to(marker)
