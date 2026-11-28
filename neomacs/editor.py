"""The Editor: the root object of a running engine, and the `ed` proxy."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import os
import signal
import socket
import sys
import threading
import traceback
import types
from collections.abc import Callable, Coroutine, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs import _owner, paths
from neomacs.errors import NoFrame, Quit
from neomacs.ownership import caller_module
from neomacs.rpc.protocol import Connection
from neomacs.rpc.transport import Listener, socket_transport, stdio_transport
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.dispatch import Dispatcher
    from neomacs.journal import Journal
    from neomacs.proc import Completed, Process
    from neomacs.session import Session
    from neomacs.text.buffer import Buffer
    from neomacs.timers import Timer
    from neomacs.window import Frame, View, Window

_current: contextvars.ContextVar[Editor] = contextvars.ContextVar("neomacs_editor")
# The frame the running code belongs to, and what a command has pinned (ENGINE 7.7).
frame_var: contextvars.ContextVar[Any] = contextvars.ContextVar("neomacs_frame", default=None)
pins_var: contextvars.ContextVar[Any] = contextvars.ContextVar("neomacs_pins", default=None)


@provisional("0.1")
@dataclass
class TaskInfo:
    """A background task, as listed in `ed.tasks`."""

    name: str
    task: asyncio.Task[Any]
    progress: str = ""
    owner: str = ""  # the module that started it (ENGINE D30)

    def cancel(self) -> None:
        self.task.cancel()


@provisional("0.1")
class Editor:
    def __init__(self) -> None:
        # The loop that serves the editor, and the one doing so at this moment:
        # the debugger's while the main thread is paused (ENGINE 10.1).
        self.main_loop: asyncio.AbstractEventLoop | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self.main_thread: int | None = None
        # Lines from every connection, in arrival order, for whichever loop
        # owns the editor to handle.  `_route_lock` orders appends against
        # the debugger's hand-over.
        self._inbox: collections.deque[tuple[Connection, bytes | None, float]] = collections.deque()
        self._route_lock = threading.Lock()
        self.idle_requests: list[Any] = []
        self.last_error: BaseException | None = None
        self.connections: set[Connection] = set()
        self.messages: list[str] = []
        self.listen_path: Path | None = None
        self.scratch = types.ModuleType("neomacs_scratch")
        self.scratch.__dict__["ed"] = self
        # State that survives re-evaluation (`neomacs.live.keep`).
        self.kept: dict[str, Any] = {}
        # Redefinitions that could not be applied in place: (module, name, reason).
        self.eval_warnings: list[tuple[str, str, str]] = []
        self.on_connection: list[Callable[[Connection], None]] = []
        self._stop: asyncio.Event | None = None
        self._context: contextvars.Context | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._noted: set[str] = set()
        self._conn_count = 0
        # Started with `--safe`: no extensions and no init file are loaded.
        self.safe = False
        # What went wrong loading the user's code, until a frame has been told.
        self.startup_problems: list[str] = []
        # Unsaved text is kept here while the engine serves (ENGINE 11.1).
        self.journal: Journal | None = None
        self.session: Session | None = None
        self._init_state()

    def _init_state(self) -> None:
        """Create the registries and subsystems."""
        from neomacs import commands as commands_module
        from neomacs.clipboard import KillRing
        from neomacs.dispatch import Dispatcher
        from neomacs.display.faces import FaceTable
        from neomacs.menu import Menu
        from neomacs.minibuffer import MinibufferAPI
        from neomacs.options import OptionStore, OptionView
        from neomacs.text.buffer import BufferList

        self.frames: dict[int, Frame] = {}
        self.last_frame: Frame | None = None
        self.option_store = OptionStore()
        self.options = OptionView()
        self.commands = commands_module.commands
        self.buffers = BufferList(self)
        self.buffers.new("*scratch*").set_mode("python")
        self.global_modes: list[Any] = []
        self.buffers.new("*messages*")
        self.faces = FaceTable()
        from neomacs.display.resources import Resources

        self.resources = Resources()
        self.faces.on_change.append(self.update_frames)
        self.frame_detach_timeout = 600.0
        self.main_dispatcher = Dispatcher(self)
        self._active_dispatcher = self.main_dispatcher
        from neomacs.debug.pause import Debugger

        self.debugger = Debugger(self)
        self.minibuffer = MinibufferAPI(self)
        self.kill_ring = KillRing()
        self.menu = Menu()
        from neomacs.std import install_menu

        install_menu(self.menu)
        self.histories: dict[str, list[str]] = {}
        # Key-to-flush times in seconds, collected while this is a list
        # (`python -m neomacs.tools.bench`); None when not measuring.
        self.latency: list[float] | None = None
        # Round-trip times of requests to frontends, in seconds, likewise.
        self.request_times: list[float] | None = None
        self.tasks: list[TaskInfo] = []
        from neomacs.project import Projects
        from neomacs.timers import Timers

        self.timers = Timers(self)
        self.processes: set[Process] = set()  # started with `spawn` and still running
        self.projects = Projects(self)
        from neomacs.workspace import Workspaces

        self.workspaces = Workspaces(self)
        # The file tree (WORKSPACE 5.4): a number that goes up whenever the
        # listings a frontend holds may be stale, and git's answers by root.
        self.files_generation = 0
        self.files_status: dict[Any, Any] = {}
        # What git says of each project's repository, by project id, as the
        # git panel is told it (WORKSPACE 5.5); and the generation it was asked at.
        self.git_states: dict[int, dict[str, Any] | None] = {}
        self.git_asked: dict[int, int] = {}
        from neomacs.ext.registry import Extensions

        self.extensions = Extensions(self)
        # How many module evaluations `neomacs.tools.reevaluate` has done here.
        self.reevaluated = 0

    # -- which loop is serving ------------------------------------------------

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop that owns the editor now."""
        return self._active_loop

    @loop.setter
    def loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.main_loop = self._active_loop = loop

    @property
    def dispatcher(self) -> Dispatcher:
        """The dispatcher of the loop that owns the editor now."""
        return self._active_dispatcher

    def activate(self, loop: asyncio.AbstractEventLoop, dispatcher: Dispatcher) -> None:
        """Route input and scheduling to LOOP from now on (the debugger's hand-over)."""
        with self._route_lock:
            self._active_loop = loop
            self._active_dispatcher = dispatcher

    def after_handover(self) -> None:
        """Called on the thread that has just become the owner."""
        loop = self._active_loop
        assert loop is not None
        for frame in list(self.frames.values()):
            frame.reschedule()
        loop.call_soon(self._drain)
        from neomacs.rpc import ui

        ui.adopt_idle_requests(self)

    def post_line(self, conn: Connection, line: bytes | None, arrived: float = 0.0) -> None:
        """Called on a reader thread: a line from CONN, or None for end of input."""
        with self._route_lock:
            self._inbox.append((conn, line, arrived))
            loop = self._active_loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop has closed
                loop.call_soon_threadsafe(self._drain)

    def _drain(self) -> None:
        if threading.get_ident() != _owner.owner():
            return  # woken on a loop that is not the owner now; the owner will drain
        while self._inbox:
            try:
                conn, line, arrived = self._inbox.popleft()
            except IndexError:
                return
            conn.deliver(line, arrived)

    # -- context (ENGINE 7.7) -------------------------------------------------

    @property
    @provisional("0.1")
    def frame(self) -> Frame | None:
        """The frame the running code belongs to: a command's own, the one a task
        inherited, or failing those the most recently used.  None if there is none."""
        frame = frame_var.get()
        if frame is not None and frame.id in self.frames:
            return frame
        if self.last_frame is not None and self.last_frame.id in self.frames:
            return self.last_frame
        return next(iter(self.frames.values()), None)

    def _need_frame(self) -> Frame:
        frame = self.frame
        if frame is None:
            raise NoFrame("no frame is attached")
        return frame

    @property
    @provisional("0.1")
    def view(self) -> View:
        """The view a command was started in, or outside a command the frame's focused view."""
        pins = pins_var.get()
        if pins is not None and pins[0].frame.owns(pins[0]):
            return pins[0]
        return self._need_frame().focus

    @property
    @provisional("0.1")
    def window(self) -> Window:
        """The main-area window: the focused one, or the last that was."""
        pins = pins_var.get()
        if pins is not None and pins[1].frame.owns(pins[1]):
            return pins[1]
        return self._need_frame().window

    @property
    @provisional("0.1")
    def buffer(self) -> Buffer:
        pins = pins_var.get()
        if pins is not None and pins[0].frame.owns(pins[0]):
            return pins[0].buffer
        return self._need_frame().focus.buffer

    @contextlib.contextmanager
    @provisional("0.1")
    def using(self, view: View) -> Generator[View]:
        """Pin the context to VIEW for a block."""
        frame = view.frame
        tokens = (frame_var.set(frame), pins_var.set((view, frame.window, view.buffer)))
        try:
            yield view
        finally:
            frame_var.reset(tokens[0])
            pins_var.reset(tokens[1])

    # -- messages and errors --------------------------------------------------

    @provisional("0.1")
    def message(self, text: str, kind: str = "info", *, frame: Frame | None = None) -> None:
        """Tell the user something: it goes to `*messages*` and to the frame concerned.

        KIND "log" is recorded without being shown.
        """
        self.messages.append(text)
        if len(self.messages) > 10000:
            del self.messages[:5000]
        log = self.buffers.get("*messages*")
        if log is not None and threading.get_ident() == (_owner.owner() or threading.get_ident()):
            with contextlib.suppress(Exception):
                log.insert(len(log), text.rstrip("\n") + "\n")
                log.saved_tick = log.tick
        target = frame if frame is not None else self.frame
        if target is not None and kind != "log":
            target.show_message(text.split("\n", 1)[0], kind)

    def note_once(self, text: str) -> None:
        if text not in self._noted:
            self._noted.add(text)
            self.message(text, kind="log")

    def report_exception(
        self, where: str, exc: BaseException, *, frame: Frame | None = None, keep: bool = False
    ) -> None:
        """Tell the user about EXC.  With KEEP it also becomes the error that
        `show_traceback` and `debug.inspect` look at."""
        self.message(f"Error {where}: {type(exc).__name__}: {exc}", kind="error", frame=frame)
        self.message("".join(traceback.format_exception(exc)), kind="log")
        if not keep:
            return
        with contextlib.suppress(Exception):
            from neomacs.live import run

            run.record_error(exc)  # for show_traceback and debug.inspect

    def guard(self, where: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Call FN, reporting any exception instead of propagating it (ENGINE 3)."""
        try:
            return fn(*args)
        except Exception as exc:
            self.report_exception(where, exc)
            return None

    async def aguard(self, where: str, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Quit:
            return None
        except Exception as exc:
            self.report_exception(where, exc)
            return None

    def update_frames(self) -> None:
        """Have every frame check whether its frontend needs anything."""
        for frame in list(self.frames.values()):
            frame.schedule_update()

    # -- commands and input ---------------------------------------------------

    @provisional("0.1")
    async def call(self, name: str, *args: Any) -> Any:
        """Run the command NAME as a key would, in the current frame."""
        return await self.dispatcher.call(self._need_frame(), name, args)

    @provisional("0.1")
    async def read_key(self) -> str:
        """Wait for the next key in the current frame, giving up the lane meanwhile."""
        frame = self._need_frame()
        assert self.loop is not None
        future: asyncio.Future[str] = self.loop.create_future()
        frame.key_waiters.append(future)
        try:
            return await self.dispatcher.wait(future)
        finally:
            if future in frame.key_waiters:
                frame.key_waiters.remove(future)

    @provisional("0.1")
    def show(self, buffer: Buffer, role: str = "document", *, focus: bool = False) -> View:
        """Show BUFFER where its ROLE belongs: the `placement` option says
        where that is (ENGINE 8.5).  The view gets the keyboard only if FOCUS."""
        from neomacs import panels

        frame = self._need_frame()
        if role == "document":
            # A buffer is shown in its own project: one that belongs to
            # another takes the frame there (WORKSPACE 4.1).
            owner = self.workspaces.of(buffer)
            if owner is not None and owner is not frame.workspace:
                frame.switch(owner)
        return panels.place(frame, buffer, role, focus=focus)

    # -- tasks ----------------------------------------------------------------

    def keep_task(self, task: asyncio.Task[Any]) -> None:
        """Hold a reference to TASK until it finishes."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @provisional("0.1")
    def start_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        frame: Frame | None = None,
        owner: str | None = None,
    ) -> TaskInfo:
        """Run COROUTINE in the background, listed in `ed.tasks`.

        It inherits the current frame and nothing else: its view, window and
        buffer are looked up each time they are read.  The module that
        started it is recorded, and its tasks are cancelled if it is disabled.
        """
        owner = owner if owner is not None else caller_module()
        assert self.loop is not None
        context = contextvars.copy_context()
        context.run(pins_var.set, None)
        context.run(frame_var.set, frame if frame is not None else frame_var.get())
        label = name or getattr(coro, "__qualname__", "task")
        task = self.loop.create_task(self.aguard(f"in task {label}", coro), context=context)
        info = TaskInfo(label, task, owner=owner)
        self.tasks.append(info)
        self.keep_task(task)

        def done(_: asyncio.Task[Any]) -> None:
            if info in self.tasks:
                self.tasks.remove(info)
            self.update_frames()

        task.add_done_callback(done)
        return info

    # -- timers, processes, threads (ENGINE 11) ----------------------------------

    @provisional("0.1")
    def after(self, seconds: float, fn: Callable[[], Any]) -> Timer:
        """Call FN once, SECONDS from now.  Returns a handle with `cancel()`.

        FN is called on the main thread; if it raises, that is reported and
        nothing else is disturbed.  It may be a coroutine function, which is
        then run as a background task.
        """
        return self.timers.add("after", seconds, fn, caller_module())

    @provisional("0.1")
    def every(self, seconds: float, fn: Callable[[], Any]) -> Timer:
        """Call FN every SECONDS, until the handle returned is cancelled.

        Setting a timer for the same named function again replaces the
        earlier one, so a line that sets one can be evaluated twice.
        """
        return self.timers.add("every", seconds, fn, caller_module())

    @provisional("0.1")
    def when_idle(self, seconds: float, fn: Callable[[], Any]) -> Timer:
        """Call FN each time the user has done nothing for SECONDS: once for
        each such pause, until the handle returned is cancelled."""
        return self.timers.add("idle", seconds, fn, caller_module())

    @provisional("0.1")
    async def run(
        self,
        argv: Sequence[str | Path],
        *,
        input: str | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        timeout: float | None = None,
    ) -> Completed:
        """Run the program ARGV to completion and return its exit status and
        what it wrote, as text.

        INPUT is given to it as standard input.  ENV is variables to add to
        the engine's own.  With CHECK, a failure status raises `ProcessError`;
        so does not finishing within TIMEOUT seconds, after the program has
        been killed.  Cancelling the command that is waiting kills it too.
        """
        from neomacs import proc

        return await proc.run(argv, input=input, cwd=cwd, env=env, check=check, timeout=timeout)

    @provisional("0.1")
    def spawn(
        self,
        argv: Sequence[str | Path],
        *,
        on_output: Callable[[str], Any] | None = None,
        on_exit: Callable[[Process], Any] | None = None,
        buffer: Buffer | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        input: bool = False,
    ) -> Process:
        """Start the program ARGV and return at once with a handle on it.

        What it writes, to standard output or standard error, is added to the
        end of BUFFER and passed to ON_OUTPUT as text, as it arrives.
        ON_EXIT is called with the handle when it has exited.  With INPUT,
        `handle.write(text)` sends it standard input.  It is listed in
        `ed.tasks`, and is killed if it is still running when the engine stops.
        """
        from neomacs import proc

        return proc.spawn(
            self, argv, on_output=on_output, on_exit=on_exit, buffer=buffer, cwd=cwd, env=env,
            input=input, owner=caller_module(),
        )  # fmt: skip

    @provisional("0.1")
    async def in_thread[T](self, fn: Callable[..., T], *args: Any) -> T:
        """Run FN(*ARGS) in a worker thread and return its result.

        For work that takes long and needs nothing of the editor's: give it
        values, such as a buffer's text, not the buffer.  Editor state may be
        touched only by the thread that owns it.
        """
        return await asyncio.to_thread(fn, *args)

    def call_from_thread(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule FN on the loop, in the editor's context, from any thread."""
        assert self._context is not None
        with self._route_lock:
            loop = self._active_loop
        assert loop is not None
        # A copy: the context itself may be in use by code the debugger has paused.
        loop.call_soon_threadsafe(fn, *args, context=self._context.copy())

    # -- lifecycle ------------------------------------------------------------

    def serve_forever(
        self,
        *,
        listen: Path | None = None,
        stdio: bool = False,
        ready: Callable[[], None] | None = None,
    ) -> None:
        """Run the engine until it is stopped."""
        with using_editor(self):
            asyncio.run(self.serve(listen=listen, stdio=stdio, ready=ready))

    def _on_sigint(self, signum: int, frame: Any) -> None:
        """SIGINT: either the fallback interrupt for a command that never awaits
        (ENGINE 3), or a real Ctrl-C asking the engine to stop."""
        dispatcher = self.dispatcher
        if dispatcher.want_interrupt:
            dispatcher.want_interrupt = False
            if dispatcher.in_foreground_command():
                raise asyncio.CancelledError("interrupted")
            return
        self.stop()

    async def serve(
        self,
        *,
        listen: Path | None = None,
        stdio: bool = False,
        ready: Callable[[], None] | None = None,
    ) -> None:
        self.loop = asyncio.get_running_loop()
        self.main_thread = threading.get_ident()
        self._stop = asyncio.Event()
        self._context = contextvars.copy_context()
        _owner.claim()
        self.main_dispatcher.start()
        self.debugger.install()
        sys.modules.setdefault(self.scratch.__name__, self.scratch)
        self._fill_scratch()
        self.timers.start()
        from neomacs import startup
        from neomacs.journal import Journal

        self.journal = Journal(self, paths.journal_dir())
        with contextlib.suppress(NotImplementedError, ValueError):
            self.loop.add_signal_handler(signal.SIGTERM, self.stop)
        previous_sigint: Any = None
        if threading.current_thread() is threading.main_thread():
            previous_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        listener: Listener | None = None
        try:
            if os.environ.get("NEOMACS_REEVALUATE"):
                # Gate D: live-patch the standard modules before serving anyone.
                from neomacs.tools import reevaluate

                await reevaluate.all_modules()
            # The user's own code has the last word (EXTENSIONS 6.1).  It is
            # loaded before any connection is opened, on a socket or on
            # stdio, so that no client is served by an engine half configured.
            await startup.load_user_code(self)
            # What was open last time (WORKSPACE 9), likewise before anyone
            # is served.  The user's code has had its say on whether.
            from neomacs import session

            self.session = session.Session(self, paths.data_dir() / session.FILE)
            if self.options.session_restore:
                self.guard("restoring the session", self.session.restore)
            self.session.start()
            if listen is not None:
                listener = Listener(listen, self._accept_from_thread)
                self.listen_path = listen
                listener.start()
            if stdio:
                self._add_connection(Connection(self, stdio_transport()))
            if ready is not None:
                ready()
            await self._stop.wait()
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
            if listener is not None:
                listener.close()
            for conn in list(self.connections):
                conn.close(drain=1.0)
            self.timers.close()
            for process in list(self.processes):
                process.kill()
            self.guard("closing the recovery journal", self.journal.close)
            if self.session is not None:
                self.guard("writing the session", self.session.close)
            for task in list(self._tasks):
                task.cancel()
            self.debugger.uninstall()
            _owner.release()

    @provisional("0.1")
    def toggle_global_mode(self, mode: Any, on: bool | None = None) -> bool:
        """Turn a global minor mode on or off.  Returns whether it is on now."""
        existing = next((m for m in self.global_modes if type(m) is mode), None)
        want = existing is None if on is None else on
        if want and existing is None:
            self.global_modes.append(mode())
        elif not want and existing is not None:
            self.global_modes.remove(existing)
        self.update_frames()
        return want

    def _fill_scratch(self) -> None:
        """Give the scratch module the public API, so it need not be imported."""
        from neomacs import commands, completion, hooks, keymap, live, modes, options, selection

        names: dict[str, Any] = {
            "asyncio": asyncio,
            "command": commands.command,
            "Ask": commands.Ask,
            "Prefix": commands.Prefix,
            "RawPrefix": commands.RawPrefix,
            "KeyText": commands.KeyText,
            "Selection": selection.Selection,
            "global_map": keymap.global_map,
            "Keymap": keymap.Keymap,
            "Hook": hooks.Hook,
            "Option": options.Option,
            "Mode": modes.Mode,
            "Candidate": completion.Candidate,
            "keep": live.keep,
            "forget": live.forget,
        }
        for name, value in names.items():
            self.scratch.__dict__.setdefault(name, value)

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self.debugger.paused:
            # The main loop cannot see the request until the paused command is let go.
            self.debugger.resume_from_anywhere("abort")

    def _accept_from_thread(self, sock: socket.socket) -> None:
        self.call_from_thread(self._accept, sock)

    def _accept(self, sock: socket.socket) -> None:
        self._conn_count += 1
        self._add_connection(Connection(self, socket_transport(sock, f"conn{self._conn_count}")))

    def _add_connection(self, conn: Connection) -> None:
        self.connections.add(conn)
        conn.on_close.append(self.connections.discard)
        for hook in list(self.on_connection):
            self.guard("connection hook", hook, conn)
        conn.start()


def current() -> Editor:
    """The editor for the calling context."""
    return _current.get()


@contextlib.contextmanager
def using_editor(editor: Editor) -> Generator[Editor]:
    token = _current.set(editor)
    try:
        yield editor
    finally:
        _current.reset(token)


class _EdProxy:
    """`ed`: attribute access is forwarded to the current editor."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_current.get(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_current.get(), name, value)

    def __repr__(self) -> str:
        return f"<ed proxy for {_current.get(None)!r}>"


ed: Any = _EdProxy()

# Importing these registers RPC methods, standard commands and default key bindings.
from neomacs import journal as _journal  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs import panels as _panels  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs import proc as _proc  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs import project as _project  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs import startup as _startup  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs import std as _std  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs.debug import ui as _debug_ui  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs.rpc import ui as _ui  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
