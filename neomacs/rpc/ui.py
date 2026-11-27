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
    places: list[Any] = options.get("places") or []
    frame.places = [str(p) for p in places]
    editor.last_frame = frame
    # An interrupt must be seen while the loop is busy, so it is spotted on the
    # reader thread before the line is queued (ENGINE 3).
    dispatcher = editor.dispatcher

    def spot_interrupt(line: bytes) -> None:
        if b'"input.interrupt"' in line:
            dispatcher.interrupt_from_thread()

    conn.intercept = spot_interrupt
    from neomacs.hooks import ui_attached

    ui_attached.run(frame)
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


def _input(conn: Connection, kind: str, value: Any) -> None:
    frame = _frame(conn)
    conn.editor.last_frame = frame
    conn.editor.dispatcher.put(frame, kind, value)


@method("input.key")
def key(conn: Connection, key: str) -> None:
    """One key, in the engine's key notation."""
    if conn.editor.latency is not None and conn.received_at:
        _frame(conn).key_times.append(conn.received_at)
    _input(conn, "key", key)


@method("input.text")
def text(conn: Connection, text: str) -> None:
    """Committed text from an input method."""
    _input(conn, "text", text)


@method("input.paste")
def paste(conn: Connection, text: str, which: str = "clipboard") -> None:
    """A paste the frontend started itself: a menu item, a middle click."""
    _input(conn, "paste", {"text": text, "which": which})


@method("input.mouse")
def mouse(
    conn: Connection,
    button: int,
    action: str,
    clicks: int,
    mods: str,
    view: int,
    area: str,
    line: int,
    offset: int = 0,
) -> None:
    """A mouse event, already hit-tested by the frontend.  It becomes a key such as
    `<mouse-1>`, `M-<drag-mouse-1>` or `<gutter-mouse-1>` (ENGINE 4.2)."""
    name = f"mouse-{int(button)}"
    if action == "drag":
        name = "drag-" + name
    elif action == "release":
        name = "up-" + name
    elif clicks >= 3:
        name = "triple-" + name
    elif clicks == 2:
        name = "double-" + name
    if area.startswith("gutter"):
        name = "gutter-" + name
    elif area == "modeline":
        name = "modeline-" + name
    prefix = "".join(f"{m}-" for m in "CMSs" if m in mods)
    event = {
        "key": f"{prefix}<{name}>",
        "view": view,
        "area": area,
        "line": line,
        "offset": offset,
        "clicks": clicks,
        "button": button,
        "action": action,
    }
    _input(conn, "mouse", event)


@method("input.drop")
def drop(conn: Connection, paths: list[str], view: int, line: int = 1, offset: int = 0) -> None:
    """Files were dropped on a view."""
    _input(conn, "drop", {"paths": paths, "view": view, "line": line, "offset": offset})


@method("input.focus")
def focus(conn: Connection, gained: bool) -> None:
    """The frontend's window gained or lost focus."""
    if gained:
        conn.editor.last_frame = _frame(conn)


@method("input.interrupt")
def interrupt(conn: Connection) -> None:
    """Out-of-band cancel.  It is acted on from the reader thread (see `attach`);
    by the time it is dispatched here there is nothing left to do."""


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
        busy = editor.dispatcher.busy or any(f.busy for f in editor.frames.values())
        quiet = 0 if busy else quiet + 1
        if loop.time() > deadline:
            return False
    return True
