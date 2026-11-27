"""Standard commands: files, buffers, running commands by name, frames."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

from neomacs import paths
from neomacs.commands import Ask, command
from neomacs.editor import ed
from neomacs.errors import NeomacsError


@command(name="files.find")
def find_file(path: Annotated[Path, Ask.file("Find file: ")]) -> None:
    """Visit PATH in a buffer."""
    if Path(path).expanduser().is_dir():
        raise NeomacsError(f"{path} is a directory")
    ed.show(ed.buffers.visit(path), focus=True)


@command(name="files.save")
async def save_file() -> None:
    """Write the buffer to its file, asking for one if it has none."""
    buffer = ed.buffer
    if buffer.file is None:
        text = await ed.minibuffer.read("Write file: ", history="files")
        buffer.file = Path(text).expanduser().resolve()
    buffer.save()
    ed.message(f"Wrote {buffer.file}")


@command(name="files.save_as")
def save_file_as(path: Annotated[Path, Ask.file("Write file: ")]) -> None:
    """Write the buffer to PATH, which becomes its file."""
    ed.buffer.save(Path(path).expanduser().resolve())
    ed.message(f"Wrote {ed.buffer.file}")


@command(name="buffers.switch")
def switch_buffer(name: Annotated[str, Ask.buffer("Switch to buffer: ")]) -> None:
    """Show the buffer called NAME in the current window, creating it if need be."""
    buffer = ed.buffers.get(name)
    if buffer is None:
        buffer = ed.buffers.new(name)
    ed.show(buffer, focus=True)


@command(name="buffers.kill")
def kill_buffer() -> None:
    """Kill the buffer that has the keyboard.  A window showing it shows
    another instead; a panel tab showing it is closed."""
    buffer = ed.buffer
    others = [b for b in ed.buffers if b is not buffer and not b.name.startswith("*minibuffer")]
    scratch = ed.buffers.get("*scratch*")
    replacement = others[-1] if others else scratch
    if replacement is None or replacement is buffer:
        raise NeomacsError("This is the only buffer")
    for frame in list(ed.frames.values()):
        for view in list(frame.views.values()):
            if view.buffer is not buffer:
                continue
            panel: Any = getattr(view, "panel", None)
            if panel is not None:
                panel.close(view)
            else:
                view.show(replacement)
    ed.buffers.kill(buffer)


@command
async def execute_command(name: Annotated[str, Ask.command("M-x ")]) -> None:
    """Run a command by name."""
    await ed.call(name)


@command
def new_frame() -> None:
    """Ask the frontend to open another window."""
    frame = ed.frame
    if frame is None or frame.conn is None:
        raise NeomacsError("No frontend is attached")
    frame.conn.notify("ui.open_frame", {})


@command
def drop() -> None:
    """Visit the files that were dropped on the window."""
    event: Any = ed.frame.last_event if ed.frame is not None else None
    paths: list[Any] = []
    if isinstance(event, dict):
        paths = cast(dict[str, Any], event).get("paths", [])
    for path in paths:
        ed.show(ed.buffers.visit(str(path)), focus=True)


@command(name="init.open")
def open_init_file() -> None:
    """Visit the init file, which is loaded each time the editor starts.
    Evaluating its buffer applies it now."""
    ed.show(ed.buffers.visit(paths.init_file()), focus=True)


@command(name="project.reload_settings")
def reload_project_settings() -> None:
    """Read every project's `.neomacs/settings.toml` again, including one
    made since its files were opened."""
    projects = ed.projects.reload()
    count = len(projects)
    ed.message(f"Read {count} project settings file{'' if count == 1 else 's'}")


@command(name="engine.exit")
def exit_engine() -> None:
    """Stop the engine."""
    ed.stop()
