"""Standard commands: showing and hiding the regions of the workbench (WORKSPACE 5.1)."""

from __future__ import annotations

from neomacs import workbench
from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.window import Frame

_NAMES = {
    "meta": "meta bar",
    "project": "project sidebar",
    "details": "details sidebar",
    "options": "options sidebar",
    "status": "status bar",
}


def _frame() -> Frame:
    frame: Frame = ed.frame
    if not frame.draws_workbench:
        raise NeomacsError("This frontend draws no sidebars or bars")
    return frame


def _toggle(name: str) -> None:
    frame = _frame()
    shown = not frame.regions[name]["shown"]
    workbench.set_region(frame, name, shown)
    ed.message(f"The {_NAMES[name]} is {'shown' if shown else 'hidden'}")


@command(name="view.toggle_meta_bar")
def toggle_meta_bar() -> None:
    """Show or hide the meta bar, the strip across the top of the window."""
    _toggle("meta")


@command(name="view.toggle_project_sidebar")
def toggle_project_sidebar() -> None:
    """Show or hide the project sidebar, the list of projects at the left."""
    _toggle("project")


@command(name="view.toggle_details_sidebar")
def toggle_details_sidebar() -> None:
    """Show or hide the details sidebar: the project's files and its git status."""
    _toggle("details")


@command(name="view.toggle_options_sidebar")
def toggle_options_sidebar() -> None:
    """Show or hide the options sidebar, at the right."""
    _toggle("options")


@command(name="view.toggle_status_bar")
def toggle_status_bar() -> None:
    """Show or hide the status bar's items.  Messages are shown there either way."""
    _toggle("status")


@command(name="view.toggle_workbench")
def toggle_workbench() -> None:
    """Hide everything around the main area, or bring it back as it was by default."""
    frame = _frame()
    any_shown = any(region["shown"] for region in frame.regions.values())
    for name in workbench.REGIONS:
        shown = not any_shown and name not in workbench.HIDDEN_AT_FIRST
        workbench.set_region(frame, name, shown)
    ed.message("Only the text is shown" if any_shown else "The bars and sidebars are shown")


@command(name="view.toggle_bottom_panel")
def toggle_bottom_panel() -> None:
    """Show or hide the panel under the main area."""
    frame: Frame = ed.frame
    panel = frame.panels.get("bottom")
    if panel is None or not panel.tabs:
        raise NeomacsError("There is nothing in the bottom panel")
    if panel.visible:
        panel.hide()
    else:
        panel.reveal()
