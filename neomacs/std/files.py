"""Standard commands: files, buffers, running commands by name, frames."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

from neomacs import paths
from neomacs.commands import Ask, RawPrefix, command
from neomacs.completion import all_buffers_completer, buffers_completer
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
async def switch_buffer(name: str = "", everywhere: Annotated[Any, RawPrefix] = None) -> None:
    """Show the buffer called NAME in the current window, creating one of
    that name if need be.  Asked for, the choice is among this project's
    buffers; with a prefix argument, among all of them, and one that is in
    another project is shown there."""
    if not name:
        completer = all_buffers_completer if everywhere is not None else buffers_completer
        prompt = "Switch to buffer: " if everywhere is None else "Switch to buffer (any project): "
        name = await ed.minibuffer.read(
            prompt, completer=completer, history="buffers", submit_selects=True
        )
    buffer = ed.buffers.get(name)
    if buffer is None:
        buffer = ed.buffers.new(name)
    ed.show(buffer, focus=True)


@command(name="buffers.kill")
def kill_buffer() -> None:
    """Kill the buffer that has the keyboard.  Its tab goes from every window;
    a window showing it shows the tab beside it, or another buffer; a panel
    tab showing it is closed."""
    buffer = ed.buffer
    scratch = ed.buffers.get("*scratch*")
    if buffer is scratch and len(ed.buffers) == 1:
        raise NeomacsError("This is the only buffer")
    for frame in list(ed.frames.values()):
        # What a window shows instead is another buffer of the same project.
        others = [b for b in ed.workspaces.buffers(frame.workspace) if b is not buffer]
        replacement = others[-1] if others else scratch
        if replacement is None or replacement is buffer:
            replacement = ed.buffers.new("*scratch*")
        for view in list(frame.views.values()):
            panel: Any = getattr(view, "panel", None)
            if panel is None:
                view.forget(buffer, replacement)  # a window loses the tab too
            elif view.buffer is buffer:
                panel.close(view)
    ed.buffers.kill(buffer)  # windows of layouts that are not on screen are seen to there


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
