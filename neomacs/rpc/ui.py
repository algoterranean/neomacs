"""RPC methods a frontend calls: attaching, viewports, acknowledgements, input (ENGINE 4.2)."""

from __future__ import annotations

import asyncio
from typing import Any

from neomacs import workbench
from neomacs.errors import NoFrame, RpcError
from neomacs.rpc.api import CAPABILITIES, PROTOCOL, method
from neomacs.rpc.protocol import Connection
from neomacs.window import Frame, Split, Window

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
    before = target.viewport
    target.viewport = (max(1, int(first)), max(1, int(last)), max(1, int(page_lines)))
    if frame.diff is not None and (before is None or before[0] != target.viewport[0]):
        frame.diff.scrolled(target)  # the other side of a diff keeps up (WORKSPACE 8)
    frame.schedule_update()


@method("view.ack")
def ack(conn: Connection, seq: int) -> None:
    """The batch whose `flush` carried SEQ has been applied."""
    _frame(conn).ack(seq)


def _input(conn: Connection, kind: str, value: Any) -> None:
    frame = _frame(conn)
    conn.editor.last_frame = frame
    conn.editor.timers.activity()
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
        frame = _frame(conn)
        editor = conn.editor
        editor.last_frame = frame
        # The user has been somewhere else, and may have copied something
        # there: look, without anything waiting on the answer (KILLRING 5.2).
        editor.start_task(editor.kill_ring.capture(frame), name="clipboard", frame=frame)
        if frame.draws_workbench:
            # Files may have been made, changed or removed meanwhile too.
            from neomacs import filetree

            filetree.changed(editor)


@method("files.list")
async def files_list(conn: Connection, workspace: int, directory: str = "") -> Any:
    """The entries of a directory of a project, for the file tree (WORKSPACE
    6).  DIRECTORY is relative to the project's root, "" for the root."""
    from neomacs import filetree

    found = conn.editor.workspaces.get(int(workspace))
    if found is None or found.root is None:
        raise RpcError(f"no such project: {workspace}")
    try:
        return await filetree.list_directory(conn.editor, found, str(directory))
    except (OSError, ValueError) as error:
        raise RpcError(str(error)) from None


@method("ui.region")
def region(
    conn: Connection, name: str, shown: bool | None = None, width: float | None = None
) -> None:
    """The user showed, hid or resized a region of the workbench (WORKSPACE 6)."""
    from neomacs import workbench

    frame = _frame(conn)
    if name in frame.regions:
        workbench.set_region(frame, name, shown, width)


@method("ui.status")
async def status(conn: Connection, side: str, index: int) -> None:
    """The user clicked an item of the status bar (WORKSPACE 6)."""
    from neomacs import workbench

    await workbench.run_status_item(_frame(conn), str(side), int(index))


@method("input.interrupt")
def interrupt(conn: Connection) -> None:
    """Out-of-band cancel.  It is acted on from the reader thread (see `attach`);
    by the time it is dispatched here there is nothing left to do."""


@method("ui.panel")
def panel(conn: Connection, panel: int, action: str, arg: Any = None) -> None:
    """The user acted on a panel in the frontend: `activate_tab` or `close_tab`
    (ARG is the tab's view), `hide`, `dock` (ARG is a side), `float`, or
    `resize` (ARG is the fraction of the frame it should take).  The
    engine stays the authority and answers with a new `layout`."""
    frame = _frame(conn)
    target = next((p for p in frame.panels.values() if p.id == panel), None)
    if target is None:
        raise RpcError(f"no such panel: {panel}")
    if action in ("activate_tab", "close_tab"):
        view = frame.views.get(int(arg))
        if view is None or view not in target.tabs:
            raise RpcError(f"panel {target.name} has no tab {arg}")
        if action == "activate_tab":
            target.activate(view)
            frame.focus_view(view)
        else:
            target.close(view)
    elif action == "hide":
        target.hide()
    elif action == "dock":
        target.dock(str(arg))
    elif action == "float":
        target.float(arg or "center")
    elif action == "resize":
        try:
            target.size = max(0.08, min(0.9, float(arg)))
        except TypeError, ValueError:
            raise RpcError(f"not a size: {arg!r}") from None
        frame.schedule_update()
    else:
        raise RpcError(f"unknown panel action: {action}")
    conn.editor.last_frame = frame


@method("ui.tab")
def tab(conn: Connection, view: int, action: str, index: Any, to: Any = None) -> None:
    """The user acted on a tab of a window (WORKSPACE 7): `activate` it,
    `close` it, or `move` it to the place TO.  INDEX is the tab's place in
    the strip the frontend was last sent."""
    frame = _frame(conn)
    window = frame.views.get(view)
    if not isinstance(window, Window) or window not in frame.windows():
        raise RpcError(f"no such window: {view}")
    if not isinstance(index, int) or not 0 <= index < len(window.tabs):
        raise RpcError(f"window {view} has no tab {index}")
    if action == "activate":
        # The keyboard goes to the window: a question left open could not be answered.
        workbench.give_up_questions(frame)
        window.show(window.tabs[index])
        frame.focus_view(window)
    elif action == "close":
        try:
            frame.close_tab(window, window.tabs[index])
        except NoFrame:
            frame.show_message("This is the only tab")
    elif action == "move":
        if not isinstance(to, int) or not 0 <= to < len(window.tabs):
            raise RpcError(f"window {view} has no place {to}")
        window.move_tab(index, to)
    else:
        raise RpcError(f"unknown tab action: {action}")
    conn.editor.last_frame = frame


@method("view.split_resized")
def split_resized(conn: Connection, split: int, ratios: list[float]) -> None:
    """The user dragged a divider: the children of SPLIT now share its space
    in RATIOS.  The engine answers with a new `layout`."""
    frame = _frame(conn)

    def find(node: Any) -> Any:
        if not isinstance(node, Split):
            return None
        if node.id == split:
            return node
        for child in node.children:
            found = find(child)
            if found is not None:
                return found
        return None

    target = find(frame.root)
    if target is None:
        raise RpcError(f"no such split: {split}")
    try:
        values = [float(r) for r in ratios]
    except TypeError, ValueError:
        raise RpcError("ratios must be numbers") from None
    if len(values) != len(target.children) or any(v <= 0 for v in values):
        raise RpcError(f"split {split} needs {len(target.children)} positive ratios")
    total = sum(values)
    target.ratios = [v / total for v in values]
    frame.schedule_update()


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
