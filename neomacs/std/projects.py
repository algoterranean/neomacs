"""Standard commands: projects (WORKSPACE.md section 4.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from neomacs.commands import Ask, command
from neomacs.completion import Candidate, FunctionCompleter, rank
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.workspace import Workspace


def _names(text: str) -> list[Candidate]:
    here = ed.frame.workspace
    found: list[Candidate] = []
    for workspace in ed.workspaces:
        where = str(workspace.root) if workspace.root is not None else ""
        note = "current" if workspace is here else where
        found.append(Candidate(workspace.name, note))
    # With nothing typed, the order of the list, which is the user's own.
    return rank(found, text) if text else found


async def _ask(prompt: str, given: int | str = "") -> Workspace:
    """The project GIVEN names, by its id or its name; asked for if none is given."""
    name: int | str = given
    if not given:
        name = await ed.minibuffer.read(
            prompt, completer=FunctionCompleter(_names), history="projects", submit_selects=True
        )
    workspace = ed.workspaces.get(name)
    if workspace is None:
        raise NeomacsError(f"There is no project called {name}")
    return workspace


@command(name="project.add")
def add_project(path: Annotated[Path, Ask.file("Add project (a directory): ")]) -> None:
    """Make a directory a project, and show it."""
    workspace = ed.workspaces.add(Path(path).expanduser())
    ed.frame.switch(workspace)
    ed.message(f"Project {workspace.name}: {workspace.root}")


@command(name="project.switch")
async def switch_project(project: int | str = "") -> None:
    """Show another project.  This one's windows and buffers are left as they
    are, to come back to."""
    workspace = await _ask("Switch to project: ", project)
    ed.frame.switch(workspace)
    ed.message(f"Project {workspace.name}")


def _step(delta: int) -> None:
    frame = ed.frame
    every = list(ed.workspaces)
    if len(every) < 2:
        raise NeomacsError("There is no other project")
    workspace = every[(every.index(frame.workspace) + delta) % len(every)]
    frame.switch(workspace)
    ed.message(f"Project {workspace.name}")


@command(name="project.next")
def next_project() -> None:
    """Show the next project in the list."""
    _step(1)


@command(name="project.previous")
def previous_project() -> None:
    """Show the previous project in the list."""
    _step(-1)


@command(name="project.rename")
async def rename_project(project: int | str = "", name: str = "") -> None:
    """Change the name a project is shown by: the current one, unless another is named."""
    workspace = await _ask("Rename project: ", project) if project else ed.frame.workspace
    if not workspace.removable:
        raise NeomacsError(f"{workspace.name} cannot be renamed")
    if not name:
        name = await ed.minibuffer.read(
            f"Rename project {workspace.name} to: ", initial=workspace.name
        )
    ed.workspaces.rename(workspace, name)
    ed.message(f"Project {workspace.name}")


@command(name="project.remove")
async def remove_project(project: int | str = "") -> None:
    """Take a project out of the list.  Its buffers are closed; its files are
    not touched.  Refused while it has unsaved buffers."""
    workspace = await _ask("Remove project: ", project)
    ed.workspaces.remove(workspace)
    ed.message(f"Removed the project {workspace.name}")


@command(name="project.move")
async def move_project(project: int | str = "", index: int = 0) -> None:
    """Put a project at another place in the list."""
    workspace = await _ask("Move project: ", project)
    ed.workspaces.move(workspace, int(index))
