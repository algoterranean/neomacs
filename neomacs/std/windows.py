"""Standard commands: windows and panels (ENGINE 8.2, 8.5)."""

from __future__ import annotations

from typing import Any

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.panels import SIDES, Panel


def _need_splits() -> None:
    if not ed.window.frame.draws_panels:
        raise NeomacsError("This frontend shows one window only")


@command
def split_window_below() -> None:
    """Divide the window in two, one above the other."""
    _need_splits()
    ed.window.frame.split(ed.window, "v")


@command
def split_window_right() -> None:
    """Divide the window in two, side by side."""
    _need_splits()
    ed.window.frame.split(ed.window, "h")


@command
def delete_window() -> None:
    """Remove this window.  Its buffer stays open."""
    frame = ed.window.frame
    if len(frame.windows()) == 1:
        raise NeomacsError("This is the only window")
    frame.delete_window(ed.window)


@command
def delete_other_windows() -> None:
    """Make this window the only one in the main area."""
    frame = ed.window.frame
    keep = ed.window
    for window in frame.windows():
        if window is not keep:
            frame.delete_window(window)


@command
def other_view() -> None:
    """Move to the next window, or the next visible panel."""
    frame = ed.frame
    if frame is None:
        return
    views = frame.views_in_order()
    if len(views) < 2:
        raise NeomacsError("There is nowhere else to go")
    here = views.index(frame.focus) if frame.focus in views else -1
    frame.focus_view(views[(here + 1) % len(views)])


def _panel(name: str | None = None) -> Panel:
    frame = ed.frame
    if frame is None or not frame.panels:
        raise NeomacsError("There are no panels")
    if name:
        found = frame.panels.get(name)
        if found is None:
            raise NeomacsError(f"No panel called {name}")
        return found
    own: Any = getattr(frame.focus, "panel", None)
    if own is not None:
        return own
    shown = [p for p in frame.panels.values() if p.visible and p.tabs]
    candidates = shown or [p for p in frame.panels.values() if p.tabs]
    if not candidates:
        raise NeomacsError("There are no panels")
    return candidates[-1]


@command(name="panel.toggle")
def panel_toggle(name: str = "") -> None:
    """Hide the panel, or show it again with the tabs it had."""
    panel = _panel(name)
    if panel.visible:
        panel.hide()
    else:
        panel.reveal()


@command(name="panel.focus")
def panel_focus(name: str = "") -> None:
    """Move into the panel; from inside it, back to the window."""
    frame = ed.frame
    assert frame is not None
    panel = _panel(name)
    if frame.focus in panel.tabs:
        frame.focus_view(frame.window)
    elif panel.active is not None:
        panel.reveal()
        frame.focus_view(panel.active)


@command(name="panel.next_tab")
def panel_next_tab(name: str = "") -> None:
    """Show the panel's next tab."""
    panel = _panel(name)
    if len(panel.tabs) < 2 or panel.active is None:
        raise NeomacsError("The panel has no other tab")
    following = panel.tabs[(panel.tabs.index(panel.active) + 1) % len(panel.tabs)]
    had_focus = panel.frame.focus in panel.tabs
    panel.activate(following)
    if had_focus:
        panel.frame.focus_view(following)


@command(name="panel.close_tab")
def panel_close_tab(name: str = "") -> None:
    """Close the panel's active tab.  The buffer it showed stays open."""
    panel = _panel(name)
    if panel.active is not None:
        panel.close(panel.active)


@command(name="panel.dock")
def panel_dock(side: str = "", name: str = "") -> None:
    """Dock the panel to a side of the frame; without a side, to the next one round."""
    panel = _panel(name)
    if not side:
        side = (
            SIDES[(SIDES.index(panel.side) + 1) % len(SIDES)]
            if panel.mode == "docked"
            else panel.side
        )
    if side not in SIDES:
        raise NeomacsError(f"Not a side: {side}")
    panel.dock(side)


@command(name="panel.float")
def panel_float(name: str = "") -> None:
    """Float the panel over the main area."""
    _panel(name).float("center")


@command
def show_messages() -> None:
    """Show the log of messages."""
    buffer = ed.buffers.get("*messages*")
    if buffer is None:
        return
    view = ed.show(buffer, "messages", focus=True)
    view.selections.set([(len(buffer), len(buffer))])
    view.reveal()
