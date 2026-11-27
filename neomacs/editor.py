"""The Editor: the root object of a running engine, and the `ed` proxy."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import signal
import socket
import sys
import threading
import traceback
import types
from collections.abc import Callable, Coroutine, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs import _owner
from neomacs.errors import NoFrame, Quit
from neomacs.rpc.protocol import Connection
from neomacs.rpc.transport import Listener, socket_transport, stdio_transport
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer
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

    def cancel(self) -> None:
        self.task.cancel()


@provisional("0.1")
class Editor:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connections: set[Connection] = set()
        self.messages: list[str] = []
        self.listen_path: Path | None = None
        self.scratch = types.ModuleType("neomacs_scratch")
        self.scratch.__dict__["ed"] = self
        self.on_connection: list[Callable[[Connection], None]] = []
        self._stop: asyncio.Event | None = None
        self._context: contextvars.Context | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._noted: set[str] = set()
        self._conn_count = 0
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
        self.buffers.new("*scratch*")
        self.buffers.new("*messages*")
        self.faces = FaceTable()
        self.faces.on_change.append(self.update_frames)
        self.frame_detach_timeout = 600.0
        self.dispatcher = Dispatcher(self)
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
        if pins is not None and pins[0].id in pins[0].frame.views:
            return pins[0]
        return self._need_frame().focus

    @property
    @provisional("0.1")
    def window(self) -> Window:
        """The main-area window: the focused one, or the last that was."""
        pins = pins_var.get()
        if pins is not None and pins[1].id in pins[1].frame.views:
            return pins[1]
        return self._need_frame().window

    @property
    @provisional("0.1")
    def buffer(self) -> Buffer:
        pins = pins_var.get()
        if pins is not None and pins[0].id in pins[0].frame.views:
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
        self, where: str, exc: BaseException, *, frame: Frame | None = None
    ) -> None:
        self.message(f"Error {where}: {type(exc).__name__}: {exc}", kind="error", frame=frame)
        self.message("".join(traceback.format_exception(exc)), kind="log")

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
        """Show BUFFER where its ROLE belongs (ENGINE 8.5).

        Until panels exist every role is placed in the main area: a window
        already showing the buffer is used, otherwise the current window.
        """
        frame = self._need_frame()
        target: View = frame.window
        if target.buffer is not buffer:
            target.show(buffer)
        if focus:
            frame.focus_view(target)
        return target

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
    ) -> TaskInfo:
        """Run COROUTINE in the background, listed in `ed.tasks`.

        It inherits the current frame and nothing else: its view, window and
        buffer are looked up each time they are read.
        """
        assert self.loop is not None
        context = contextvars.copy_context()
        context.run(pins_var.set, None)
        context.run(frame_var.set, frame if frame is not None else frame_var.get())
        label = name or getattr(coro, "__qualname__", "task")
        task = self.loop.create_task(self.aguard(f"in task {label}", coro), context=context)
        info = TaskInfo(label, task)
        self.tasks.append(info)
        self.keep_task(task)

        def done(_: asyncio.Task[Any]) -> None:
            if info in self.tasks:
                self.tasks.remove(info)
            self.update_frames()

        task.add_done_callback(done)
        return info

    def call_from_thread(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule FN on the loop, in the editor's context, from any thread."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(fn, *args, context=self._context)

    # -- lifecycle ------------------------------------------------------------

    def run(
        self,
        *,
        listen: Path | None = None,
        stdio: bool = False,
        ready: Callable[[], None] | None = None,
    ) -> None:
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
        self._stop = asyncio.Event()
        self._context = contextvars.copy_context()
        _owner.claim()
        self.dispatcher.start()
        sys.modules.setdefault(self.scratch.__name__, self.scratch)
        with contextlib.suppress(NotImplementedError, ValueError):
            self.loop.add_signal_handler(signal.SIGTERM, self.stop)
        previous_sigint: Any = None
        if threading.current_thread() is threading.main_thread():
            previous_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        listener: Listener | None = None
        try:
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
            for task in list(self._tasks):
                task.cancel()
            _owner.release()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

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
from neomacs import std as _std  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
from neomacs.rpc import ui as _ui  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
