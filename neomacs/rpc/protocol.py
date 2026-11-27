"""JSON-RPC 2.0 over a transport (ENGINE 4.1).

Either side may send requests, so each side numbers its own ids.  All
dispatch happens on the engine's loop; the transport's threads only hand
lines over.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import threading
import time
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from neomacs.errors import FrontendGone, RpcError
from neomacs.rpc import api
from neomacs.rpc.transport import Transport

if TYPE_CHECKING:
    from neomacs.editor import Editor

_log_lock = threading.Lock()
_log_path = os.environ.get("NEOMACS_RPC_LOG")


def _log(direction: str, name: str, text: str) -> None:
    if not _log_path:
        return
    with _log_lock, open(_log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.time():.6f} {direction} {name} {text}\n")


class Connection:
    """One client.  Created and used on the loop thread."""

    def __init__(self, editor: Editor, transport: Transport) -> None:
        self.editor = editor
        self.transport = transport
        self.name = transport.name
        self.closed = False
        self.frame: Any = None  # set by ui.attach
        self.on_close: list[Callable[[Connection], None]] = []
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Called on the reader thread, before the line is queued for the loop.
        self.intercept: Callable[[bytes], None] | None = None
        loop = editor.loop
        assert loop is not None
        self._loop = loop
        self._context = contextvars.copy_context()
        transport.on_line = self._line_from_thread
        transport.on_close = self._close_from_thread

    def start(self) -> None:
        self.transport.start()

    # -- reader thread ----------------------------------------------------

    def _line_from_thread(self, line: bytes) -> None:
        if self.intercept is not None:
            self.intercept(line)
        try:
            self._loop.call_soon_threadsafe(self._on_line, line, context=self._context)
        except RuntimeError:  # loop closed
            pass

    def _close_from_thread(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_closed, context=self._context)
        except RuntimeError:
            pass

    # -- loop thread ------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        _log(">", self.name, text)
        self.transport.send(text.encode("utf-8"))

    def notify(self, method: str, *params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": list(params)})

    async def request(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        """Send a request to the client and await its reply."""
        if self.closed:
            raise FrontendGone(f"{self.name} has disconnected")
        msgid = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = self._loop.create_future()
        self._pending[msgid] = future
        self._send({"jsonrpc": "2.0", "id": msgid, "method": method, "params": list(params)})
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msgid, None)

    def close(self, drain: float = 0.0) -> None:
        self.transport.close(drain)

    def _on_closed(self) -> None:
        if self.closed:
            return
        # Every line read before the end of input has been handled by now,
        # so their replies are queued; let them out before closing.
        self.transport.close(drain=0.5)
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(FrontendGone(f"{self.name} has disconnected"))
        self._pending.clear()
        for callback in list(self.on_close):
            self.editor.guard(f"close of {self.name}", callback, self)

    def _on_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        _log("<", self.name, text)
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            self._send(_error_reply(None, -32700, f"parse error: {exc}"))
            return
        if not isinstance(message, dict):
            self._send(_error_reply(None, -32600, "invalid request"))
            return
        msg: dict[str, Any] = message  # pyright: ignore[reportUnknownVariableType]
        if "method" in msg:
            self._on_call(msg)
        elif "id" in msg:
            self._on_reply(msg)

    def _on_reply(self, msg: dict[str, Any]) -> None:
        future = self._pending.get(msg["id"])
        if future is None or future.done():
            return
        if msg.get("error") is not None:
            err = msg["error"]
            future.set_exception(
                RpcError(str(err.get("message", "error")), err.get("code", -32000), err.get("data"))
            )
        else:
            future.set_result(msg.get("result"))

    def _on_call(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        params: list[Any] = msg.get("params") or []
        msgid = msg.get("id")
        is_request = "id" in msg
        handler = api.lookup(method)
        if handler is None:
            if is_request:
                self._send(_error_reply(msgid, -32601, f"method not found: {method}"))
            else:
                self.editor.note_once(f"unknown notification: {method}")
            return
        try:
            inspect.signature(handler).bind(self, *params)
        except TypeError as exc:
            if is_request:
                self._send(_error_reply(msgid, -32602, f"invalid params for {method}: {exc}"))
            else:
                self.editor.message(f"invalid params for {method}: {exc}")
            return
        try:
            result = handler(self, *params)
        except Exception as exc:
            self._failed(method, msgid, is_request, exc)
            return
        finally:
            # Whatever the call did (move a cursor, change a face) may need showing.
            self.editor.update_frames()
        if inspect.isawaitable(result):
            task = self._loop.create_task(self._finish(method, msgid, is_request, result))
            self.editor.keep_task(task)
        elif is_request:
            self._send({"jsonrpc": "2.0", "id": msgid, "result": result})

    async def _finish(self, method: str, msgid: Any, is_request: bool, awaitable: Any) -> None:
        try:
            result = await awaitable
        except Exception as exc:
            self._failed(method, msgid, is_request, exc)
            return
        finally:
            self.editor.update_frames()
        if is_request:
            self._send({"jsonrpc": "2.0", "id": msgid, "result": result})

    def _failed(self, method: str, msgid: Any, is_request: bool, exc: Exception) -> None:
        if not isinstance(exc, RpcError):
            self.editor.report_exception(f"handling {method}", exc)
        if is_request:
            self._send(_exception_reply(msgid, exc))


def _error_reply(msgid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msgid, "error": error}


def _exception_reply(msgid: Any, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RpcError):
        return _error_reply(msgid, exc.code, str(exc), exc.data)
    data = {
        "type": type(exc).__qualname__,
        "traceback": "".join(traceback.format_exception(exc)),
    }
    return _error_reply(msgid, -32000, f"{type(exc).__name__}: {exc}", data)
