"""Processes (ENGINE 11): run a program and take its output, or leave one
running and be told what it writes."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs.errors import NeomacsError
from neomacs.options import Option
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.text.buffer import Buffer

CHUNK = 65536

Option(
    "process_output_limit",
    int,
    2_000_000,
    "Characters of a process's output kept in its buffer; older output is dropped.",
    validate=lambda n: n >= 1000,
)


@provisional("0.1")
class ProcessError(NeomacsError):
    """A program run with `check=True` exited with a failure status, or one
    given a time limit did not finish in it."""

    def __init__(self, message: str, completed: Completed | None = None) -> None:
        super().__init__(message)
        self.completed = completed


@provisional("0.1")
@dataclass(frozen=True)
class Completed:
    """What a program run with `ed.run` did."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@provisional("0.1")
def shell_argv(command: str) -> list[str]:
    """The argument list that runs COMMAND in the user's shell."""
    if sys.platform == "win32":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
    return [os.environ.get("SHELL") or "/bin/sh", "-c", command]


def _argv(argv: Sequence[str | Path]) -> list[str]:
    if isinstance(argv, (str, bytes)):
        raise TypeError(
            "a program is given as a list of arguments; "
            "for a shell command line use neomacs.proc.shell_argv(text)"
        )
    args = [str(arg) for arg in argv]
    if not args:
        raise ValueError("no program to run")
    return args


def _environment(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The engine's environment with ENV's variables over it."""
    return None if env is None else {**os.environ, **env}


# A child gets a process group of its own, so that stopping it stops what it
# started too (the programs of a shell pipeline).
_GROUP: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}


def _signal(process: asyncio.subprocess.Process, signum: int, *, group: bool = False) -> None:
    """Send SIGNUM to PROCESS and what it started.  With GROUP, also when the
    process itself has exited: a shell can exit and leave a program it put in
    the background holding the output open."""
    if process.returncode is not None and not group:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform != "win32":
            os.killpg(process.pid, signum)
        elif signum == getattr(signal, "SIGKILL", -1):
            process.kill()
        else:
            process.terminate()


_SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


async def run(
    argv: Sequence[str | Path],
    *,
    input: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> Completed:
    """Run a program to completion and return its output as text.

    Cancelling the caller (C-g on a command waiting for this) kills the program.
    """
    args = _argv(argv)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_environment(env),
        **_GROUP,
    )
    data = input.encode("utf-8", errors="surrogateescape") if input is not None else None
    try:
        async with asyncio.timeout(timeout):
            out, err = await process.communicate(data)
    except TimeoutError:
        _signal(process, _SIGKILL, group=True)
        await process.wait()
        raise ProcessError(f"{args[0]} did not finish in {timeout:g} seconds") from None
    except BaseException:
        _signal(process, _SIGKILL, group=True)
        with contextlib.suppress(BaseException):
            await asyncio.shield(process.wait())
        raise
    done = Completed(
        tuple(args),
        process.returncode if process.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )
    if check and done.returncode != 0:
        last = done.stderr.strip().rsplit("\n", 1)[-1]
        raise ProcessError(
            f"{args[0]} exited with status {done.returncode}" + (f": {last}" if last else ""), done
        )
    return done


@provisional("0.1")
class Process:
    """A program started with `ed.spawn`.  It runs until it exits or is stopped."""

    def __init__(
        self,
        editor: Editor,
        argv: list[str],
        *,
        on_output: Callable[[str], Any] | None,
        on_exit: Callable[[Process], Any] | None,
        buffer: Buffer | None,
    ) -> None:
        self.editor = editor
        self.argv = argv
        self.on_output = on_output
        self.on_exit = on_exit
        self.buffer = buffer
        self.pid: int | None = None
        # The exit status once it has exited; negative for a signal.
        self.returncode: int | None = None
        # Why it could not be started, if it could not.
        self.error: BaseException | None = None
        self.owner = ""  # the module that started it
        self._process: asyncio.subprocess.Process | None = None
        self._done: asyncio.Event = asyncio.Event()
        self._unsent: list[bytes] = []
        self._close_input = False
        self._stop: int | None = None  # a signal asked for before it had started

    @property
    @provisional("0.1")
    def running(self) -> bool:
        return not self._done.is_set()

    @provisional("0.1")
    def write(self, text: str) -> None:
        """Send TEXT to the program's standard input.  It must have been
        started with `input=True`."""
        data = text.encode("utf-8", errors="surrogateescape")
        process = self._process
        if process is None:
            self._unsent.append(data)
        elif process.stdin is not None and not process.stdin.is_closing():
            process.stdin.write(data)

    @provisional("0.1")
    def close_input(self) -> None:
        """Tell the program there is no more input."""
        process = self._process
        if process is None:
            self._close_input = True
        elif process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                process.stdin.close()

    def _send(self, signum: int) -> None:
        if self._process is None:
            self._stop = signum
        elif self.running:
            # Still being served, so something of its group may be alive even
            # if the program first started is not.
            _signal(self._process, signum, group=True)

    @provisional("0.1")
    def interrupt(self) -> None:
        """What Ctrl-C does in a terminal."""
        self._send(signal.SIGINT)

    @provisional("0.1")
    def terminate(self) -> None:
        """Ask the program to stop."""
        self._send(signal.SIGTERM)

    @provisional("0.1")
    def kill(self) -> None:
        """Stop the program, whether it likes it or not."""
        self._send(_SIGKILL)

    @provisional("0.1")
    async def wait(self) -> int:
        """Wait for the program to exit; returns its exit status."""
        await self._done.wait()
        return self.returncode if self.returncode is not None else -1

    # -- the task that looks after it ------------------------------------------

    async def _serve(
        self, cwd: str | Path | None, env: Mapping[str, str] | None, input: bool
    ) -> None:
        editor = self.editor
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.argv,
                    stdin=asyncio.subprocess.PIPE if input else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=_environment(env),
                    **_GROUP,
                )
            except (OSError, ValueError) as exc:
                self.error = exc
                self.returncode = 127
                self._deliver(f"Could not run {self.argv[0]}: {exc}\n")
                return
            self._process = process
            self.pid = process.pid
            for data in self._unsent:
                self.write(data.decode("utf-8", errors="surrogateescape"))
            self._unsent.clear()
            if self._close_input:
                self.close_input()
            if self._stop is not None:
                _signal(process, self._stop)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            assert process.stdout is not None
            try:
                while True:
                    data = await process.stdout.read(CHUNK)
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        self._deliver(text)
                rest = decoder.decode(b"", final=True)
                if rest:
                    self._deliver(rest)
                self.returncode = await process.wait()
            except BaseException:
                # The task was cancelled, or the engine is stopping: take the program with it.
                _signal(process, _SIGKILL, group=True)
                with contextlib.suppress(BaseException):
                    self.returncode = await asyncio.shield(process.wait())
                raise
        finally:
            if self.returncode is None:
                self.returncode = -1
            self._done.set()
            editor.processes.discard(self)
            if self.on_exit is not None:
                result = editor.guard(f"when {self.argv[0]} exited", self.on_exit, self)
                if asyncio.iscoroutine(result):
                    editor.start_task(result, name=f"exit of {self.argv[0]}")
            editor.update_frames()

    def _deliver(self, text: str) -> None:
        text = text.replace("\r\n", "\n")
        editor = self.editor
        if self.buffer is not None:
            editor.guard(f"showing the output of {self.argv[0]}", self._insert, self.buffer, text)
        if self.on_output is not None:
            result = editor.guard(f"handling the output of {self.argv[0]}", self.on_output, text)
            if asyncio.iscoroutine(result):
                result.close()
                editor.message(
                    f"The output function given for {self.argv[0]} is a coroutine; "
                    "it is called without being awaited, so it must be a plain function",
                    kind="error",
                )
        editor.update_frames()

    def _insert(self, buffer: Buffer, text: str) -> None:
        """Add TEXT at the end of BUFFER.  A view whose cursor was at the end stays there."""
        end = len(buffer)
        following = [
            view
            for frame in self.editor.frames.values()
            for view in frame.views.values()
            if view.buffer is buffer and view.cursor.pos == end
        ]
        read_only, buffer.read_only = buffer.read_only, False
        try:
            buffer.insert(end, text)
            limit = int(self.editor.options.process_output_limit)
            if len(buffer) > limit + limit // 4:
                keep_from = len(buffer) - limit
                cut = buffer.line_start(buffer.line_of(keep_from))
                # At a line's start where there is one to be had; a single
                # line longer than the limit is cut where it must be.
                buffer.delete(0, cut if cut > 0 else keep_from)
        finally:
            buffer.read_only = read_only
        # Output is not an edit: nothing to undo, nothing to save.
        buffer.undo.clear()
        buffer.saved_tick = buffer.tick
        for view in following:
            view.selections.set([(len(buffer), len(buffer))])
            view.reveal(len(buffer), "bottom")


def spawn(
    editor: Editor,
    argv: Sequence[str | Path],
    *,
    on_output: Callable[[str], Any] | None = None,
    on_exit: Callable[[Process], Any] | None = None,
    buffer: Buffer | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: bool = False,
    owner: str = "",
) -> Process:
    args = _argv(argv)
    process = Process(editor, args, on_output=on_output, on_exit=on_exit, buffer=buffer)
    process.owner = owner
    editor.processes.add(process)
    editor.start_task(
        process._serve(cwd, env, input),  # pyright: ignore[reportPrivateUsage]
        name=f"process {args[0]}",
        owner=owner,
    )
    return process
