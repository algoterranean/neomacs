"""Standard commands: running programs (ENGINE 11)."""

from __future__ import annotations

from typing import Annotated, Any

from neomacs.commands import Ask, command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap
from neomacs.live import run as live
from neomacs.proc import Process, shell_argv

OUTPUT = "*shell output*"


class ProcessMode(live.SpecialMode):
    """The mode of a buffer a program's output goes to."""

    name = "process"
    interactive = False
    keymap = Keymap("process")


assert ProcessMode.keymap is not None
ProcessMode.keymap.bind("C-c C-c", "process.interrupt")
ProcessMode.keymap.bind("C-c C-k", "process.kill")


def _process() -> Process:
    found: Any = ed.buffer.locals.get("process")
    if not isinstance(found, Process) or not found.running:
        raise NeomacsError("No program is running here")
    return found


@command
def shell_command(
    line: Annotated[str, Ask.string("Shell command: ", history="shell")],
) -> None:
    """Run a command line in the shell.  Its output appears in a buffer as it
    is written, and the editor can be used meanwhile."""
    if not line.strip():
        raise NeomacsError("No command given")
    origin = ed.buffer
    previous: Any = None
    existing = ed.buffers.get(OUTPUT)
    if existing is not None:
        previous = existing.locals.get("process")
    if isinstance(previous, Process) and previous.running:
        raise NeomacsError(f"A command is still running in {OUTPUT}; C-c C-k there kills it")
    buffer = live.special_buffer(OUTPUT, ProcessMode)
    buffer.insert(0, f"$ {line}\n")
    live.finish_special(buffer)

    def finished(process: Process) -> None:
        status = process.returncode
        if process.error is not None:
            ed.message(f"Could not run the shell: {process.error}", kind="error")
        elif status == 0:
            ed.message(f"Finished: {line}")
        elif status is not None and status < 0:
            ed.message(f"Stopped by signal {-status}: {line}", kind="warning")
        else:
            ed.message(f"Exited with status {status}: {line}", kind="warning")

    # Where the file being edited is, as a shell started there would be.
    cwd = origin.file.parent if origin.file is not None and origin.file.parent.is_dir() else None
    buffer.locals["process"] = ed.spawn(shell_argv(line), buffer=buffer, cwd=cwd, on_exit=finished)
    view = ed.show(buffer, "output")
    view.selections.set([(len(buffer), len(buffer))])


@command(name="process.interrupt")
def interrupt_process() -> None:
    """Interrupt the program running in this buffer, as Ctrl-C would in a terminal."""
    _process().interrupt()


@command(name="process.kill")
def kill_process() -> None:
    """Kill the program running in this buffer."""
    _process().kill()
