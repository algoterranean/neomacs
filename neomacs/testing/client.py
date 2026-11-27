"""A small synchronous JSON-RPC client, and a helper that runs an engine."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any


class RemoteError(Exception):
    """An error reply from the engine."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "error"))
        self.code: int = error.get("code", 0)
        self.data: Any = error.get("data")


class NotReady(Exception):
    """Raised by a request handler that cannot answer yet.  The client asks it
    again after each later notification, and replies with an error if it is
    still not ready when the time allowed runs out."""


class Client:
    """Connects to an engine socket.  Replies and notifications arrive on a thread."""

    def __init__(self, path: Path | str, *, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(str(path))
        self._file = self._sock.makefile("rb")
        self._next_id = 1
        self._replies: dict[int, queue.SimpleQueue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._defer_lock = threading.RLock()
        self.notifications: queue.SimpleQueue[tuple[str, list[Any]]] = queue.SimpleQueue()
        # Handlers for requests the engine sends us: name -> fn(*params) -> result.
        self.request_handlers: dict[str, Callable[..., Any]] = {}
        # Requests whose handler raised NotReady: (message, deadline).
        self._deferred: list[tuple[dict[str, Any], float]] = []
        self.defer_seconds = 0.25
        # Called on the reader thread for each notification, before it is queued.
        self.on_notification: Callable[[str, list[Any]], None] | None = None
        self.closed = threading.Event()
        threading.Thread(target=self._reader, name="client-reader", daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            self._sock.sendall(data)

    def _reader(self) -> None:
        try:
            for line in self._file:
                msg = json.loads(line)
                if "method" in msg:
                    if "id" in msg:
                        self._answer(msg)
                    else:
                        params: list[Any] = msg.get("params") or []
                        if self.on_notification is not None:
                            self.on_notification(msg["method"], params)
                        self.notifications.put((msg["method"], params))
                elif "id" in msg:
                    waiter = self._replies.get(msg["id"])
                    if waiter is not None:
                        waiter.put(msg)
        except OSError, ValueError:
            pass
        finally:
            self.closed.set()

    def _answer(self, msg: dict[str, Any], deadline: float | None = None) -> None:
        handler = self.request_handlers.get(msg["method"])
        if handler is None:
            reply: dict[str, Any] = {"error": {"code": -32601, "message": "method not found"}}
        else:
            try:
                reply = {"result": handler(*(msg.get("params") or []))}
            except NotReady as exc:
                import time

                if deadline is None:
                    deadline = time.monotonic() + self.defer_seconds
                if time.monotonic() < deadline:
                    self._deferred.append((msg, deadline))
                    if len(self._deferred) == 1:
                        threading.Timer(self.defer_seconds + 0.02, self.retry_deferred).start()
                    return
                reply = {"error": {"code": -32000, "message": f"not available: {exc}"}}
            except Exception as exc:
                reply = {"error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}
        self._send({"jsonrpc": "2.0", "id": msg["id"], **reply})

    def retry_deferred(self) -> None:
        """Ask the handlers of deferred requests again."""
        with self._defer_lock:
            waiting, self._deferred = self._deferred, []
            for msg, deadline in waiting:
                self._answer(msg, deadline)

    def call(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        """Send a request and return its result, raising RemoteError on an error reply."""
        with self._lock:
            msgid = self._next_id
            self._next_id += 1
        waiter: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._replies[msgid] = waiter
        self._send({"jsonrpc": "2.0", "id": msgid, "method": method, "params": list(params)})
        try:
            msg = waiter.get(timeout=timeout if timeout is not None else self.timeout)
        except queue.Empty:
            raise TimeoutError(f"no reply to {method}") from None
        finally:
            self._replies.pop(msgid, None)
        if msg.get("error") is not None:
            raise RemoteError(msg["error"])
        return msg.get("result")

    def notify(self, method: str, *params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": list(params)})

    def eval(self, source: str) -> Any:
        """Evaluate SOURCE in the engine and return the value's repr."""
        return self.call("engine.eval", source)

    def wait_notification(self, method: str, *, timeout: float | None = None) -> list[Any]:
        """Return the params of the next notification named METHOD, discarding others."""
        import time

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no {method} notification")
            try:
                name, params = self.notifications.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"no {method} notification") from None
            if name == method:
                return params

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def short_tempdir() -> Path:
    """A fresh temporary directory whose path is short enough for a socket."""
    for base in (tempfile.gettempdir(), "/tmp"):
        try:
            path = Path(tempfile.mkdtemp(prefix="nm", dir=base))
        except OSError:
            continue
        if len(os.fsencode(path)) <= 80:
            return path
        shutil.rmtree(path, ignore_errors=True)
    raise OSError("no temporary directory short enough for a socket path")


@contextlib.contextmanager
def running_engine(
    *, env: dict[str, str] | None = None, args: list[str] | None = None
) -> Generator[Path]:
    """Run an engine in a child process; yield its socket path."""
    tmp = short_tempdir()
    path = tmp / "e.sock"
    full_env = dict(os.environ)
    full_env.setdefault("NEOMACS_CHECK_OWNER", "1")
    if env:
        full_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "neomacs", "--listen", str(path), *(args or [])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line.startswith("NEOMACS-LISTENING"):
            err = proc.stderr.read() if proc.stderr is not None else ""
            raise RuntimeError(f"engine did not start: {line!r}\n{err}")
        yield path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        shutil.rmtree(tmp, ignore_errors=True)
