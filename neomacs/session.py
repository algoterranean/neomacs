"""The session (WORKSPACE.md section 9): what was open, kept across a restart.

`session.json` in the data directory holds the projects, the files open in
each, and for each frame how it had each project laid out: the window tree,
every window's tabs, where the cursor was in each.  It holds no text:
unsaved text is the recovery journal's (ENGINE 11.1), and neither depends on
the other.

It is looked at every little while and written when it has changed, and
once more when the engine stops: taken on the owner thread, written in a
worker thread, replaced whole.  At startup it is read before any frontend is
served.  Projects and buffers are made then; a frame's layout is applied to
the first frame that attaches, the second's to the second.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from neomacs.options import Option
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.window import Frame, Window
    from neomacs.workspace import Workspace

FORMAT = 1
FILE = "session.json"
MAX_FILES = 500  # of one project: more than this are not all reopened

Option(
    "session_restore",
    bool,
    True,
    "Whether the projects, files and windows that were open when the editor "
    "last stopped are opened again when it starts.",
)
Option(
    "session_delay",
    float,
    2.0,
    "Seconds between looks at whether the session has changed and needs writing.",
    validate=lambda n: n > 0,
)


def _write(path: Path, text: str) -> None:
    """Replace the session file.  Runs in a worker thread: it touches nothing of the editor's."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(tmp, path)


def _place(view: Any, buffer: Buffer) -> list[int]:
    """[line, offset] of the cursor VIEW has in BUFFER, showing or not."""
    if view.buffer is buffer:
        selections = view.selections
    else:
        selections = buffer.locals.get("_view_selections", {}).get(view.id)
    if selections is None:
        return [1, 0]
    return list(buffer.line_offset(selections.primary.head.pos))


@provisional("0.1")
class Session:
    """What is open, as it is written down and as it was read."""

    def __init__(self, editor: Editor, path: Path) -> None:
        self.editor = editor
        self.path = path
        self._written: str | None = None
        self._frames: list[dict[str, Any]] = []  # as last taken with a frame to take them from
        self._timer: Any = None
        self._io = threading.Lock()
        self._busy = False
        self._closed = False
        # Read at startup: each frame's layout, waiting for a frame to attach.
        self.pending: list[dict[str, Any]] = []
        self._projects: list[Workspace | None] = []  # by their place in what was read
        self.missing: list[str] = []
        self._told = False

    # -- taking ----------------------------------------------------------------------

    def _tab(self, view: Any, buffer: Buffer) -> list[Any] | None:
        if buffer.file is None:
            return None  # nothing to open it from next time
        return [str(buffer.file), *_place(view, buffer)]

    def _tree(self, node: Any) -> Any:
        from neomacs.window import Split

        if isinstance(node, Split):
            return {
                "split": node.direction,
                "ratios": [round(r, 4) for r in node.ratios],
                "children": [self._tree(child) for child in node.children],
            }
        tabs = [tab for tab in (self._tab(node, b) for b in node.tabs) if tab is not None]
        active = str(node.buffer.file) if node.buffer.file is not None else None
        return {"tabs": tabs, "active": active}

    def _arrangement(self, root: Any, window: Any, panels: dict[str, Any]) -> dict[str, Any]:
        from neomacs.window import Split

        leaves: list[Any] = []

        def walk(node: Any) -> None:
            if isinstance(node, Split):
                for child in node.children:
                    walk(child)
            else:
                leaves.append(node)

        walk(root)
        return {
            "tree": self._tree(root),
            "window": leaves.index(window) if window in leaves else 0,
            # A panel's tabs are the editor's own buffers, which are not
            # kept; where the user put the panel and how big is.
            "panels": [
                {
                    "name": panel.name, "mode": panel.mode, "side": panel.side,
                    "size": round(float(panel.size), 4), "transient": bool(panel.transient),
                }
                for panel in panels.values()
                if isinstance(panel.anchor, str)
            ],
        }  # fmt: skip

    def take(self) -> dict[str, Any]:
        """What is open now, as it is written.  Owner thread only."""
        editor = self.editor
        workspaces = list(editor.workspaces)
        index = {workspace.id: place for place, workspace in enumerate(workspaces)}
        projects: list[dict[str, Any]] = []
        for workspace in workspaces:
            files = [
                str(b.file) for b in editor.workspaces.buffers(workspace) if b.file is not None
            ]
            projects.append(
                {
                    "name": workspace.name,
                    "root": str(workspace.root) if workspace.root is not None else None,
                    "files": files[-MAX_FILES:],
                    "folders": sorted(workspace.folders),
                }
            )
        frames: list[dict[str, Any]] = []
        for frame in editor.frames.values():
            arrangements = {
                str(index[frame.workspace.id]): self._arrangement(
                    frame.root, frame.window, frame.panels
                )
            }
            for workspace in workspaces:
                kept = workspace.arrangements.get(frame.id)
                if kept is not None:
                    arrangements[str(index[workspace.id])] = self._arrangement(
                        kept.root, kept.window, kept.panels
                    )
            frames.append(
                {
                    "current": index[frame.workspace.id],
                    "regions": {name: dict(region) for name, region in frame.regions.items()},
                    "arrangements": arrangements,
                }
            )
        if frames:
            self._frames = frames
        # With no frame there is nothing to say of windows: the last word stands.
        return {"format": FORMAT, "projects": projects, "frames": self._frames}

    # -- writing ---------------------------------------------------------------------

    def start(self) -> None:
        """Begin looking, every little while, at whether there is something to write."""
        self._schedule()

    def _schedule(self) -> None:
        # Not one of `ed.timers`: those are the user's and their extensions' to see.
        loop = self.editor.loop
        if loop is not None and not self._closed:
            delay = float(self.editor.options.session_delay)
            self._timer = loop.call_later(delay, self._tick)

    def _tick(self) -> None:
        loop = self.editor.loop
        if loop is None or self._closed:
            return
        self.editor.keep_task(loop.create_task(self.save()))
        self._schedule()

    async def save(self) -> None:
        """Write the session if it is not what was last written."""
        if self._busy:
            return
        self._busy = True
        try:
            text = json.dumps(self.take(), indent=1)
            if text != self._written:
                await self.editor.in_thread(self._replace, text)
        except OSError as exc:
            self.editor.note_once(f"The session could not be written: {exc}")
        finally:
            self._busy = False

    def _replace(self, text: str) -> None:
        with self._io:
            _write(self.path, text)
            self._written = text

    def close(self) -> None:
        """The engine is stopping: write the session as it stands."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        with contextlib.suppress(OSError):
            text = json.dumps(self.take(), indent=1)
            if text != self._written:
                self._replace(text)

    # -- reading ---------------------------------------------------------------------

    def restore(self) -> None:
        """Open again what the file says was open.  Projects and buffers are
        made now; each frame's layout waits for a frame to attach."""
        editor = self.editor
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return
        record = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if record.get("format") != FORMAT:
            return
        self._written = json.dumps(record, indent=1)
        try:
            for project in record.get("projects", []):
                self._projects.append(self._project(project))
            self.pending = [f for f in record.get("frames", []) if isinstance(f, dict)]
            self._frames = list(self.pending)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # A file from a newer version, or one that was damaged: start afresh.
            editor.note_once(f"The session could not be read: {exc}")
            self.pending = []

    def _project(self, project: dict[str, Any]) -> Workspace | None:
        editor = self.editor
        root = project.get("root")
        if root is None:
            workspace: Workspace = editor.workspaces.none
        elif Path(root).is_dir():
            workspace = editor.workspaces.add(Path(root), str(project.get("name") or ""))
        else:
            self.missing.append(str(root))
            return None
        workspace.folders = {str(folder) for folder in project.get("folders", [])}
        for file in project.get("files", []):
            self._buffer(str(file), workspace)
        return workspace

    def _buffer(self, file: str, workspace: Workspace | None = None) -> Buffer | None:
        """The buffer for FILE, visited if it is still there to visit."""
        path = Path(file)
        existing = self.editor.buffers.find_file(path)
        if existing is not None:
            return existing
        if not path.is_file():
            if file not in self.missing:
                self.missing.append(file)
            return None
        try:
            buffer = self.editor.buffers.visit(path)
        except OSError:
            self.missing.append(file)
            return None
        if workspace is not None:
            self.editor.workspaces.adopt(buffer, workspace)
        return buffer

    # -- a frame attaches ----------------------------------------------------------------

    def attach(self, frame: Frame) -> None:
        """FRAME has just attached: if a layout is waiting, it is this frame's."""
        if frame.session_applied:
            return
        frame.session_applied = True
        if self.pending:
            record = self.pending.pop(0)
            try:
                self._apply(frame, record)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
                self.editor.note_once(
                    f"The windows of the last session could not be restored: {exc}"
                )
        if self.missing and not self._told:
            self._told = True
            gone = ", ".join(self.missing[:8]) + (" and more" if len(self.missing) > 8 else "")
            self.editor.message(f"No longer there, and not opened again: {gone}", frame=frame)

    def _apply(self, frame: Frame, record: dict[str, Any]) -> None:
        regions = record.get("regions", {})
        if frame.draws_workbench and isinstance(regions, dict):
            for name, saved in cast(dict[str, Any], regions).items():
                region = frame.regions.get(name)
                if region is None or not isinstance(saved, dict):
                    continue
                state = cast(dict[str, Any], saved)
                region["shown"] = bool(state.get("shown", region["shown"]))
                if "width" in region and isinstance(state.get("width"), (int, float)):
                    region["width"] = float(state["width"])
        current = self._workspace(record.get("current"))
        arrangements = cast(dict[str, Any], record.get("arrangements", {}))
        # The project that was showing is done last, so that it is the one left showing.
        order = sorted(arrangements, key=lambda key: self._workspace(key) is current)
        for key in order:
            workspace = self._workspace(key)
            if workspace is None:
                continue
            frame.switch(workspace)
            self._arrange(frame, cast(dict[str, Any], arrangements[key]))
        if current is not None:
            frame.switch(current)
        frame.full_sync()

    def _workspace(self, key: Any) -> Workspace | None:
        try:
            return self._projects[int(key)]
        except TypeError, ValueError, IndexError:
            return None

    def _arrange(self, frame: Frame, saved: dict[str, Any]) -> None:
        """Lay FRAME's project out as SAVED says, in place of the one window it has."""
        from neomacs.panels import Panel

        first = frame.window
        made: list[Window] = []
        tree = saved.get("tree")
        if not frame.draws_panels:
            # One window is all this frontend shows: the one that had the keyboard.
            leaves = self._leaves(tree)
            tree = leaves[min(int(saved.get("window", 0)), len(leaves) - 1)] if leaves else None
        root = self._build(frame, tree, made)
        if root is None or not made:
            return  # nothing of it is left to show
        frame.root = root
        focus = made[min(max(0, int(saved.get("window", 0))), len(made) - 1)]
        frame.window = focus
        frame.focus = focus
        if first not in made:
            first.close()
        for panel in saved.get("panels", []):
            name = str(panel["name"])
            if frame.draws_panels and name not in frame.panels:
                # Empty until something is shown in it, and then where it was.
                frame.panels[name] = Panel(
                    frame, name, mode=str(panel.get("mode", "docked")),
                    side=str(panel.get("side", "bottom")), transient=bool(panel.get("transient")),
                    size=float(panel.get("size", 0.3)),
                )  # fmt: skip
        frame.schedule_update()

    def _leaves(self, node: Any) -> list[Any]:
        if isinstance(node, dict) and "split" in node:
            found: list[Any] = []
            for child in cast(dict[str, Any], node).get("children", []):
                found.extend(self._leaves(child))
            return found
        return [node] if isinstance(node, dict) else []

    def _build(self, frame: Frame, node: Any, made: list[Window]) -> Any:
        """The window tree NODE describes, or None if nothing in it can be shown."""
        from neomacs.window import Split, Window

        if not isinstance(node, dict):
            return None
        saved = cast(dict[str, Any], node)
        if "split" in saved:
            children: list[Any] = []
            ratios: list[float] = []
            given = list(saved.get("ratios", []))
            for place, child in enumerate(saved.get("children", [])):
                built = self._build(frame, child, made)
                if built is not None:
                    children.append(built)
                    ratios.append(float(given[place]) if place < len(given) else 1.0)
            if not children:
                return None
            if len(children) == 1:
                return children[0]  # the others' files have gone
            split = Split(str(saved["split"]) if saved["split"] in ("h", "v") else "v", children)
            total = sum(ratios) or 1.0
            split.ratios = [ratio / total for ratio in ratios]
            return split
        tabs: list[tuple[Buffer, int, int]] = []
        for tab in saved.get("tabs", []):
            buffer = self._buffer(str(tab[0]))
            if buffer is not None and all(buffer is not other for other, _, _ in tabs):
                tabs.append((buffer, int(tab[1]), int(tab[2])))
        if not tabs:
            return None
        window = Window(frame, tabs[0][0])
        active = tabs[0][0]
        for buffer, line, offset in tabs:
            window.show(buffer)
            line = max(1, min(buffer.line_count, line))
            pos = min(buffer.position(line, 0) + max(0, offset), buffer.line_end(line))
            window.selections.set([(pos, pos)])
            if str(buffer.file) == saved.get("active"):
                active = buffer
        window.show(active)
        made.append(window)
        return window
