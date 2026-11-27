"""The Editor: the root object of a running engine, and the `ed` proxy."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import signal
import socket
import sys
import traceback
import types
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

from neomacs import _owner
from neomacs.rpc.protocol import Connection
from neomacs.rpc.transport import Listener, socket_transport, stdio_transport

_current: contextvars.ContextVar[Editor] = contextvars.ContextVar("neomacs_editor")


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
        """Create the registries.  Extended as subsystems are added."""
        self.frames: dict[int, Any] = {}

    # -- messages and errors ------------------------------------------------

    def message(self, text: str) -> None:
        """Record a message for the user."""
        self.messages.append(text)
        if len(self.messages) > 10000:
            del self.messages[:5000]

    def note_once(self, text: str) -> None:
        if text not in self._noted:
            self._noted.add(text)
            self.message(text)

    def report_exception(self, where: str, exc: BaseException) -> None:
        self.message(f"Error {where}: {type(exc).__name__}: {exc}")
        self.messages.append("".join(traceback.format_exception(exc)))

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
        except Exception as exc:
            self.report_exception(where, exc)
            return None

    # -- tasks ----------------------------------------------------------------

    def keep_task(self, task: asyncio.Task[Any]) -> None:
        """Hold a reference to TASK until it finishes."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def call_from_thread(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule FN on the loop, in the editor's context, from any thread."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(fn, *args, context=self._context)

    # -- lifecycle ----------------------------------------------------------------

    def run(
        self,
        *,
        listen: Path | None = None,
        stdio: bool = False,
        ready: Callable[[], None] | None = None,
    ) -> None:
        with using_editor(self):
            asyncio.run(self.serve(listen=listen, stdio=stdio, ready=ready))

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
        sys.modules.setdefault(self.scratch.__name__, self.scratch)
        with contextlib.suppress(NotImplementedError, ValueError):
            self.loop.add_signal_handler(signal.SIGTERM, self.stop)
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
