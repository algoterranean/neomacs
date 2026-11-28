"""Projects (WORKSPACE.md): several directories open in one editor, each with
its own buffers, windows and layout.

"Project" is the word in the interface.  Here the class is `Workspace`,
because `neomacs.project` already means a directory's settings file.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs.errors import NeomacsError
from neomacs.hooks import Hook
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer, buffer_created, buffer_killed

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.window import Frame, View, Window

_ids = itertools.count(1)
_OWNER = "_workspace"  # in a buffer's locals: the project it was opened in

workspace_changed = Hook(
    "workspace_changed", "Run when a frame shows another project, with (frame)."
)
workspaces_changed = Hook(
    "workspaces_changed", "Run when a project is added, removed, renamed or reordered."
)


@dataclass
class Arrangement:
    """One frame's layout for one project, while that project is not showing."""

    root: Any  # the window tree
    window: Window
    focus: View
    panels: dict[str, Any]
    views: dict[int, View] = field(default_factory=dict[int, "View"])


@provisional("0.1")
class Workspace:
    """A project: a directory, and what is open in it."""

    def __init__(self, name: str, root: Path | None) -> None:
        self.id = next(_ids)
        self.name = name
        self.root = root  # None for "No project"
        # Frame id -> how that frame had it laid out when it last showed it.
        self.arrangements: dict[int, Arrangement] = {}

    @property
    def removable(self) -> bool:
        return self.root is not None

    def __repr__(self) -> str:
        return f"<Workspace {self.name!r} {self.root}>"


def _editors_own(buffer: Buffer) -> bool:
    """Whether BUFFER is one the editor writes itself, which belongs to no project."""
    return buffer.file is None and buffer.name.startswith("*")


@provisional("0.1")
class Workspaces:
    """`ed.workspaces`: every project, in the order of the project sidebar."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self.none = Workspace("No project", None)
        self._items: list[Workspace] = []

    def __iter__(self):  # noqa: ANN204
        """The projects the user added, then "No project"."""
        return iter([*self._items, self.none])

    def __len__(self) -> int:
        return len(self._items) + 1

    # -- finding ---------------------------------------------------------------

    @provisional("0.1")
    def get(self, key: int | str) -> Workspace | None:
        """The project with this id or this name."""
        for workspace in self:
            if key in (workspace.id, workspace.name):
                return workspace
        return None

    @provisional("0.1")
    def containing(self, path: Path) -> Workspace | None:
        """The project whose root is the nearest directory above PATH, if any is."""
        best: Workspace | None = None
        for workspace in self._items:
            root = workspace.root
            if root is None or not (path == root or root in path.parents):
                continue
            if best is None or best.root is None or len(root.parts) > len(best.root.parts):
                best = workspace
        return best

    @provisional("0.1")
    def of(self, buffer: Buffer) -> Workspace | None:
        """The project BUFFER belongs to; None for one of the editor's own
        buffers, which belongs to no project and can be shown in any.

        A file under a project's root belongs to that project.  Anything else
        belongs to the project it was opened in."""
        if _editors_own(buffer):
            return None
        if buffer.file is not None:
            found = self.containing(buffer.file)
            if found is not None:
                return found
        owner: Any = buffer.locals.get(_OWNER)
        return owner if isinstance(owner, Workspace) and owner in list(self) else self.none

    @provisional("0.1")
    def buffers(self, workspace: Workspace) -> list[Buffer]:
        """The buffers that belong to WORKSPACE, oldest first."""
        return [b for b in self.editor.buffers if self.of(b) is workspace]

    @provisional("0.1")
    def unsaved(self, workspace: Workspace) -> list[Buffer]:
        return [b for b in self.buffers(workspace) if b.modified and b.file is not None]

    # -- changing --------------------------------------------------------------

    def _name(self, wanted: str, but: Workspace | None = None) -> str:
        taken = {w.name for w in self if w is not but}
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted} ({n})" in taken:
            n += 1
        return f"{wanted} ({n})"

    @provisional("0.1")
    def add(self, path: Path | str, name: str | None = None) -> Workspace:
        """Make the directory PATH a project.  One that is a project already
        is returned as it is."""
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise NeomacsError(f"{root} is not a directory")
        for existing in self._items:
            if existing.root == root:
                return existing
        workspace = Workspace(self._name(name or root.name or str(root)), root)
        self._items.append(workspace)
        self._evict(workspace)
        workspaces_changed.run()
        self.editor.update_frames()
        return workspace

    @provisional("0.1")
    def rename(self, workspace: Workspace, name: str) -> None:
        name = name.strip()
        if not name:
            raise NeomacsError("A project needs a name")
        if not workspace.removable:
            raise NeomacsError(f"{workspace.name} cannot be renamed")
        workspace.name = self._name(name, workspace)
        workspaces_changed.run()
        self.editor.update_frames()

    @provisional("0.1")
    def move(self, workspace: Workspace, index: int) -> None:
        """Put WORKSPACE at INDEX in the order of the list."""
        if workspace in self._items:
            self._items.remove(workspace)
            self._items.insert(max(0, min(index, len(self._items))), workspace)
            workspaces_changed.run()
            self.editor.update_frames()

    @provisional("0.1")
    def remove(self, workspace: Workspace) -> None:
        """Take WORKSPACE out of the list.  Its buffers are killed; its files
        are not touched.  Refused while any of its buffers is unsaved (W13)."""
        if not workspace.removable or workspace not in self._items:
            raise NeomacsError(f"{workspace.name} cannot be removed")
        unsaved = self.unsaved(workspace)
        if unsaved:
            names = ", ".join(b.name for b in unsaved)
            raise NeomacsError(
                f"{workspace.name} has unsaved buffers ({names}); save or kill them first"
            )
        editor = self.editor
        mine = self.buffers(workspace)
        for frame in list(editor.frames.values()):
            if frame.workspace is workspace:
                frame.switch(self.none)
        self._items.remove(workspace)
        for frame in list(editor.frames.values()):
            self._discard(frame, workspace)
        for buffer in mine:
            editor.buffers.kill(buffer)
        workspaces_changed.run()
        editor.update_frames()

    def _evict(self, workspace: Workspace) -> None:
        """WORKSPACE has just been added, and files that were open already are
        under its root.  They are its buffers now: a window of another project
        that shows one shows something else."""
        mine = set(self.buffers(workspace))
        if not mine:
            return
        scratch = self.editor.buffers.get("*scratch*")

        def clear(views: Any, owner: Workspace) -> None:
            for view in list(views):
                if view.buffer not in mine:
                    continue
                others = [b for b in self.buffers(owner) if b not in mine]
                replacement = others[-1] if others else scratch
                if replacement is None:
                    continue
                panel: Any = getattr(view, "panel", None)
                if panel is not None and view in panel.tabs and view.id in view.frame.views:
                    panel.close(view)
                else:
                    view.show(replacement)

        for frame in list(self.editor.frames.values()):
            clear(frame.views.values(), frame.workspace)
        for other in self:
            for arrangement in other.arrangements.values():
                clear(arrangement.views.values(), other)

    def _discard(self, frame: Frame, workspace: Workspace) -> None:
        """Forget how FRAME had WORKSPACE laid out, and close the views it kept for it."""
        arrangement = workspace.arrangements.pop(frame.id, None)
        if arrangement is None:
            return
        for view in arrangement.views.values():
            # `close` takes a view out of the frame's table, where these are not.
            view.buffer.remove_listener(view._on_change)  # pyright: ignore[reportPrivateUsage]
            view.selections.detach()

    def forget_frame(self, frame: Frame) -> None:
        """FRAME has gone: so have the layouts it kept for projects it was not showing."""
        for workspace in self:
            self._discard(frame, workspace)

    # -- what the rest of the editor tells this --------------------------------

    def adopt(self, buffer: Buffer, workspace: Workspace | None = None) -> None:
        """Note the project BUFFER was opened in, if it has not one already."""
        if _OWNER in buffer.locals or _editors_own(buffer):
            return
        if workspace is None:
            frame = self.editor.frame
            workspace = frame.workspace if frame is not None else self.none
        buffer.locals[_OWNER] = workspace

    def replace_killed(self, buffer: Buffer) -> None:
        """BUFFER is being killed.  A window that shows it, in a layout that is
        not on screen, shows something else instead."""
        scratch = self.editor.buffers.get("*scratch*")
        for workspace in self:
            for arrangement in workspace.arrangements.values():
                for view in arrangement.views.values():
                    if view.buffer is not buffer:
                        continue
                    others = [b for b in self.buffers(workspace) if b is not buffer]
                    replacement = others[-1] if others else scratch
                    if replacement is not None and replacement is not buffer:
                        view.show(replacement)


def _workspaces() -> Workspaces | None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    return getattr(editor, "workspaces", None) if editor is not None else None


def _created(buffer: Buffer) -> None:
    workspaces = _workspaces()
    if workspaces is not None:
        workspaces.adopt(buffer)


def _killed(buffer: Buffer) -> None:
    workspaces = _workspaces()
    if workspaces is not None:
        workspaces.replace_killed(buffer)


buffer_created.add(_created)
buffer_killed.add(_killed)
