"""RPC methods a frontend calls: attaching, viewports, acknowledgements, input (ENGINE 4.2)."""

from __future__ import annotations

import asyncio
from typing import Any

from neomacs.errors import RpcError
from neomacs.rpc.api import CAPABILITIES, PROTOCOL, method
from neomacs.rpc.protocol import Connection
from neomacs.window import Frame

PROTOCOL_MISMATCH = -32001
NO_SUCH_FRAME = -32002
FRAME_IN_USE = -32003


def _frame(conn: Connection) -> Frame:
    frame = conn.frame
    if not isinstance(frame, Frame):
        raise RpcError("this connection has not attached a frame")
    return frame


def _closed(conn: Connection) -> None:
    frame = conn.frame
    if isinstance(frame, Frame) and frame.conn is conn:
        frame.detach()


@method("ui.attach")
def attach(conn: Connection, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a frame for this connection, or reattach to one, and start sending `redraw`."""
    options = options or {}
    theirs = options.get("protocol")
    if theirs != PROTOCOL:
        raise RpcError(
            f"protocol mismatch: the engine speaks version {PROTOCOL}, the frontend {theirs}",
            code=PROTOCOL_MISMATCH,
            data={"engine": PROTOCOL, "frontend": theirs},
        )
    if conn.frame is not None:
        raise RpcError("this connection already has a frame")
    wanted: list[str] = list(options.get("capabilities") or [])
    capabilities = [c for c in wanted if c in CAPABILITIES]
    editor = conn.editor
    if "frame" in options:
        frame = editor.frames.get(options["frame"])
        if not isinstance(frame, Frame) or frame.token != options.get("token"):
            raise RpcError("no such frame; it may have expired", code=NO_SUCH_FRAME)
        if frame.attached:
            raise RpcError("that frame is attached to another connection", code=FRAME_IN_USE)
        frame.reattach(conn, capabilities)
    else:
        frame = Frame(editor, conn, capabilities)
    conn.frame = frame
    conn.on_close.append(_closed)
    return {
        "frame": frame.id,
        "token": frame.token,
        "protocol": PROTOCOL,
        "capabilities": capabilities,
    }


@method("ui.resync")
def resync(conn: Connection) -> None:
    """Send the full state again on this connection."""
    _frame(conn).full_sync()


@method("ui.detach")
def detach(conn: Connection) -> None:
    """Stop redraw and delete the frame."""
    frame = _frame(conn)
    conn.frame = None
    frame.delete()


@method("view.viewport")
def viewport(conn: Connection, view: int, first: int, last: int, page_lines: int) -> None:
    """The lines now visible in a view."""
    frame = _frame(conn)
    target = frame.views.get(view)
    if target is None:
        return
    target.viewport = (max(1, int(first)), max(1, int(last)), max(1, int(page_lines)))
    frame.schedule_update()


@method("view.ack")
def ack(conn: Connection, seq: int) -> None:
    """The batch whose `flush` carried SEQ has been applied."""
    _frame(conn).ack(seq)


@method("input.key")
def key(conn: Connection, key: str) -> None:
    """One key, in the engine's key notation."""
    conn.editor.input.put(_frame(conn), "key", key)


@method("input.text")
def text(conn: Connection, text: str) -> None:
    """Committed text from an input method."""
    conn.editor.input.put(_frame(conn), "text", text)


@method("input.interrupt")
def interrupt(conn: Connection) -> None:
    """Out-of-band cancel.  Nothing to cancel until commands are tasks."""


@method("engine.idle")
async def idle(conn: Connection, timeout: float = 10.0) -> bool:
    """Return once input has been handled and every responsive frame is up to date.

    For tests and scripts.  Returns False if that did not happen within TIMEOUT.
    """
    editor = conn.editor
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    quiet = 0
    while quiet < 3:
        await asyncio.sleep(0.001 if quiet == 0 else 0)
        busy = editor.input.busy or any(
            isinstance(f, Frame) and f.busy for f in editor.frames.values()
        )
        quiet = 0 if busy else quiet + 1
        if loop.time() > deadline:
            return False
    return True
