"""Standard commands: the mouse.  A mouse event is a key such as `<mouse-1>`; the
event itself is in `ed.frame.last_event`."""

from __future__ import annotations

from typing import Any, cast

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.window import View


def _target() -> tuple[View, int] | None:
    frame = ed.frame
    event: Any = frame.last_event if frame is not None else None
    if frame is None or not isinstance(event, dict):
        return None
    event = cast(dict[str, Any], event)
    view = frame.views.get(int(event.get("view", -1)))
    if view is None:
        return None
    buf = view.buffer
    line = max(1, min(buf.line_count, int(event.get("line", 1))))
    pos = buf.position(line, int(event.get("offset", 0)))
    return view, pos if buf.is_boundary(pos) else buf.prev_boundary(pos)


@command(name="mouse.set_cursor")
def set_cursor() -> None:
    """Put the one cursor where the mouse was pressed, focusing that view."""
    target = _target()
    if target is None:
        return
    view, pos = target
    view.frame.focus_view(view)
    view.selections.extending = False
    view.selections.keep_primary()
    view.selections.primary.select(pos, pos)


@command(name="mouse.drag")
def drag() -> None:
    """Extend the selection to where the mouse has been dragged."""
    target = _target()
    if target is not None:
        view, pos = target
        primary = view.selections.primary
        primary.select(primary.anchor.pos, pos)


@command(name="mouse.add_cursor")
def add_cursor() -> None:
    """Add a cursor where the mouse was pressed."""
    target = _target()
    if target is not None:
        view, pos = target
        view.frame.focus_view(view)
        view.selections.add(pos)


@command(name="mouse.select_word")
def select_word() -> None:
    """Select the word under the mouse."""
    target = _target()
    if target is not None:
        view, pos = target
        word = view.buffer.syntax.word_at(pos)
        if word is not None:
            view.selections.keep_primary()
            view.selections.primary.select(*word)


@command(name="mouse.select_line")
def select_line() -> None:
    """Select the line under the mouse."""
    target = _target()
    if target is not None:
        view, pos = target
        buf = view.buffer
        line = buf.line_of(pos)
        end = buf.line_start(line + 1) if line < buf.line_count else buf.line_end(line)
        view.selections.keep_primary()
        view.selections.primary.select(buf.line_start(line), end)
