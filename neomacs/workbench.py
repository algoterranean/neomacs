"""The workbench (WORKSPACE.md section 5): what a frontend that draws the
frame around the main area is told, beyond the views themselves.

The frontend draws the regions as its own widgets.  What they show comes from
here: which regions are open and how wide, the list of projects, and the
items of the status bar.  Sent only to a frontend that claimed the capability
`workbench` when it attached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from neomacs import vcs
from neomacs.options import Option

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.window import Frame

# Every region that can be shown and hidden, and the width in ems of those
# that have one.  The names are those of CLAUDE.md.
REGIONS = ("meta", "project", "details", "options", "status")
WIDTHS = {"project": 11.0, "details": 17.0, "options": 17.0}
HIDDEN_AT_FIRST = ("options",)  # nothing is in it yet (WORKSPACE W8)
MIN_WIDTH, MAX_WIDTH = 6.0, 60.0

Option(
    "workbench",
    bool,
    True,
    "Whether a new window shows the meta bar, the sidebars and the status bar. "
    "Each can be shown and hidden in a window afterwards.",
)


def default_regions(editor: Editor) -> dict[str, dict[str, Any]]:
    on = bool(editor.options.get("workbench", True))
    regions: dict[str, dict[str, Any]] = {}
    for name in REGIONS:
        region: dict[str, Any] = {"shown": on and name not in HIDDEN_AT_FIRST}
        if name in WIDTHS:
            region["width"] = WIDTHS[name]
        regions[name] = region
    return regions


def describe_regions(frame: Frame) -> dict[str, dict[str, Any]]:
    described = {name: dict(region) for name, region in frame.regions.items()}
    # The bottom panel is a panel like any other (ENGINE 8.5); it is here so
    # that the frontend's toggle for it can show whether it is open.
    bottom = frame.panels.get("bottom")
    described["bottom"] = {"shown": bottom is not None and bottom.visible and bool(bottom.tabs)}
    return described


def describe_workspaces(frame: Frame) -> list[dict[str, Any]]:
    editor = frame.editor
    workspaces = editor.workspaces
    unsaved: dict[int, int] = {}
    for buffer in editor.buffers:
        if buffer.modified:
            owner = workspaces.of(buffer)
            if owner is not None:
                unsaved[owner.id] = unsaved.get(owner.id, 0) + 1
    return [
        {
            "id": workspace.id,
            "name": workspace.name,
            "root": str(workspace.root) if workspace.root is not None else None,
            "current": workspace is frame.workspace,
            "unsaved": unsaved.get(workspace.id, 0),
        }
        for workspace in workspaces
    ]


_QUIET = ("timer ", "clipboard", "exit of ")  # tasks that are the editor's own business
_NEWLINES = {"\n": "LF", "\r\n": "CRLF", "\r": "CR"}


def _item(text: str, command: str | None = None, title: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"text": text}
    if command:
        item["command"] = command
    if title:
        item["title"] = title
    return item


def describe_status(frame: Frame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The status bar's items: at the left what is true of the frame, at the
    right what is true of the window that has, or last had, the keyboard."""
    editor = frame.editor
    workspace = frame.workspace
    window = frame.focus if frame.focus in frame.windows() else frame.window
    buffer = window.buffer
    left = [_item(workspace.name, "project.switch", "Switch project")]
    probe = workspace.root / "x" if workspace.root is not None else buffer.file
    branch = vcs.branch(probe)
    if branch:
        left.append(_item(f"{vcs.BRANCH_SYMBOL} {branch}", title="Branch"))
    for info in editor.tasks:
        if info.name.startswith(_QUIET) or info.task.done():
            continue
        text = f"{info.name}: {info.progress}" if info.progress else info.name
        left.append(_item(f"⟳ {text}", title="Running"))
        if len(left) >= 5:
            break
    line, _ = buffer.line_offset(window.cursor.pos)
    right = [_item(f"Ln {line}, Col {window.cursor.column + 1}", "goto_line", "Go to line")]
    count = len(window.selections)
    if count > 1:
        right.append(_item(f"{count} selections"))
    mode = buffer.mode
    if mode is not None:
        right.append(_item(mode.title().capitalize(), title="Mode"))
    right.append(_item(buffer.encoding.upper(), title="Encoding"))
    right.append(_item(_NEWLINES.get(buffer.newline, "LF"), title="Line ending"))
    return left, right


def sync(frame: Frame, body: list[Any]) -> None:
    """Append what the frontend needs to bring its workbench up to date."""
    regions = describe_regions(frame)
    if regions != frame.sent_workbench.get("regions"):
        body.append(["regions", regions])
        frame.sent_workbench["regions"] = regions
    workspaces = describe_workspaces(frame)
    if workspaces != frame.sent_workbench.get("workspaces"):
        body.append(["workspaces", workspaces])
        frame.sent_workbench["workspaces"] = workspaces
    status = describe_status(frame)
    if status != frame.sent_workbench.get("status"):
        body.append(["status", status[0], status[1]])
        frame.sent_workbench["status"] = status


def set_region(
    frame: Frame, name: str, shown: bool | None = None, width: float | None = None
) -> None:
    """Show, hide or resize a region of FRAME."""
    region = frame.regions.get(name)
    if region is None:
        raise KeyError(f"no region called {name}")
    if shown is not None:
        region["shown"] = bool(shown)
    if width is not None and "width" in region:
        region["width"] = round(max(MIN_WIDTH, min(MAX_WIDTH, float(width))), 2)
    frame.schedule_update()
