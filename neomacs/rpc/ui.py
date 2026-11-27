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

    def spot_interrupt(line: bytes) -> None:
        if b'"input.interrupt"' in line:
            # Whichever loop is serving: the debugger's while it has a command paused.
            editor.dispatcher.interrupt_from_thread()

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


class IdleRequest:
    """An `engine.idle` request that has not been answered.  The loop that
    took it may be frozen by the debugger before it can answer; the loop that
    takes over answers it instead."""

    def __init__(self, conn: Connection, msgid: Any, timeout: float) -> None:
        self.conn = conn
        self.msgid = msgid
        self.timeout = timeout
        self.answered = False


async def _settled(editor: Any, request: IdleRequest) -> bool | None:
    """Wait until the editor is idle.  None means this loop is being parked
    by the debugger and the other one must answer."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request.timeout
    quiet = 0
    debugger = editor.debugger
    while quiet < 3 and not request.answered:
        await asyncio.sleep(0.001 if quiet == 0 else 0)
        if debugger.resuming and loop is not editor.main_loop:
            return None
        busy = (
            debugger.resuming
            or editor.dispatcher.busy
            or any(f.busy for f in editor.frames.values())
        )
        quiet = 0 if busy else quiet + 1
        if loop.time() > deadline:
            return False
    return True


async def _answer_idle(editor: Any, request: IdleRequest) -> None:
    ok = await _settled(editor, request)
    if ok is not None and not request.answered:
        request.answered = True
        if request in editor.idle_requests:
            editor.idle_requests.remove(request)
        request.conn.answer(request.msgid, ok)


def adopt_idle_requests(editor: Any) -> None:
    """Answer, from the loop that has just taken over, the requests the other loop left."""
    loop = asyncio.get_running_loop()
    for request in list(editor.idle_requests):
        editor.keep_task(loop.create_task(_answer_idle(editor, request)))


@method("engine.idle")
def idle(conn: Connection, timeout: float = 10.0) -> Any:
    """Return once input has been handled and every responsive frame is up to date.

    For tests and scripts.  Returns False if that did not happen within TIMEOUT.
    While the debugger has a command paused, "handled" means paused.
    """
    # Registered at once, not when the coroutine first runs: the loop may be
    # frozen by the debugger before it gets that far.
    request = IdleRequest(conn, conn.current_id, timeout)
    conn.editor.idle_requests.append(request)
    return _idle(conn.editor, request)


async def _idle(editor: Any, request: IdleRequest) -> Any:
    from neomacs.rpc.api import NO_REPLY

    ok = await _settled(editor, request)
    if ok is None or request.answered:
        return NO_REPLY  # the loop that takes over, or took over, answers
    request.answered = True
    editor.idle_requests.remove(request)
    return ok
