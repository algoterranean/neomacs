"""Standard commands: the tabs of a window (WORKSPACE 7).

A window's tabs are the buffers that have been shown in it.  A frontend that
draws the workbench shows them as a strip above the window; the commands
work in any frontend.
"""

from __future__ import annotations

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError, NoFrame
from neomacs.window import Window


def _step(delta: int) -> None:
    window: Window = ed.window
    if len(window.tabs) < 2:
        raise NeomacsError("The window has no other tab")
    here = window.tabs.index(window.buffer)
    window.show(window.tabs[(here + delta) % len(window.tabs)])


@command(name="tabs.next")
def next_tab() -> None:
    """Show the window's next tab; after the last, the first."""
    _step(1)


@command(name="tabs.previous")
def previous_tab() -> None:
    """Show the window's previous tab; before the first, the last."""
    _step(-1)


@command(name="tabs.close")
def close_tab() -> None:
    """Close the window's tab.  The buffer stays open.  The tab to the left is
    shown; a window whose last tab is closed is removed."""
    window: Window = ed.window
    try:
        window.frame.close_tab(window)
    except NoFrame:
        raise NeomacsError("This is the only tab") from None


@command(name="tabs.close_others")
def close_other_tabs() -> None:
    """Close every tab of the window but the one it shows.  The buffers stay open."""
    window: Window = ed.window
    if len(window.tabs) < 2:
        raise NeomacsError("The window has no other tab")
    window.tabs[:] = [window.buffer]
    window.frame.schedule_update()


def _move(delta: int) -> None:
    window: Window = ed.window
    here = window.tabs.index(window.buffer)
    to = here + delta
    if not 0 <= to < len(window.tabs):
        raise NeomacsError("The tab is at the end already")
    window.move_tab(here, to)


@command(name="tabs.move_left")
def move_tab_left() -> None:
    """Move the window's tab one place to the left."""
    _move(-1)


@command(name="tabs.move_right")
def move_tab_right() -> None:
    """Move the window's tab one place to the right."""
    _move(1)


@command(name="view.toggle_tabs")
def toggle_tabs() -> None:
    """Show or hide the strip of tabs above each window."""
    shown = not ed.options.get("tabs", True)
    ed.options.tabs = shown
    ed.update_frames()
    ed.message("Tabs are shown" if shown else "Tabs are hidden")
