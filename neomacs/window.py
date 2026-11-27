"""Views, windows and frames (ENGINE 8.2 to 8.4), and what each frontend has been sent."""

from __future__ import annotations

import asyncio
import itertools
import secrets
from typing import TYPE_CHECKING, Any

from neomacs.display.faces import UI_FACES
from neomacs.display.view import build_line
from neomacs.errors import FrontendGone, NoFrame, RpcError
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer, Change
from neomacs.text.marker import Cursor, Marker

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.rpc.protocol import Connection

_view_ids = itertools.count(1)
_frame_ids = itertools.count(1)

# An edit that would send more lines than this at once is sent as a count,
# and the lines the frontend then asks to see are filled in afterwards.
SPLICE_LIMIT = 500
STALL_SECONDS = 2.0
GEOMETRY_TIMEOUT = 2.0


MAX_DIRTY_RANGES = 32


def merge_dirty(
    ranges: list[tuple[int, int, int]], line: int, removed: int, inserted: int
) -> list[tuple[int, int, int]]:
    """Add an edit to a list of changed line ranges.

    Each range is (first line now, lines there before, lines there now); they
    are sorted and do not touch.  The edit replaced REMOVED lines at LINE, in
    current numbering, with INSERTED lines.  Ranges it touches are merged with
    it, taking in the untouched lines between; ranges below it move.
    """
    above: list[tuple[int, int, int]] = []
    touched: list[tuple[int, int, int]] = []
    below: list[tuple[int, int, int]] = []
    for first, old, new in ranges:
        if first + new < line:
            above.append((first, old, new))
        elif first > line + removed:
            below.append((first + inserted - removed, old, new))
        else:
            touched.append((first, old, new))
    start, end = line, line + removed
    if touched:
        start = min(start, touched[0][0])
        end = max(end, touched[-1][0] + touched[-1][2])
    now = end - start
    before = now - sum(new - old for _, old, new in touched)
    merged = [*above, (start, before, now + inserted - removed), *below]
    merged = [r for r in merged if r[1] or r[2]]
    if len(merged) > MAX_DIRTY_RANGES:  # too fragmented to be worth tracking separately
        start = merged[0][0]
        now = merged[-1][0] + merged[-1][2] - start
        merged = [(start, now - sum(new - old for _, old, new in merged), now)]
    return merged


@provisional("0.1")
class View:
    """A buffer as shown somewhere: selections, a viewport, and sync state for one frame."""

    def __init__(self, frame: Frame, buffer: Buffer) -> None:
        self.id = next(_view_ids)
        self.frame = frame
        self.wrap = True
        self.shape = "block"
        # (first visible line, last visible line, lines per page), as last reported.
        self.viewport: tuple[int, int, int] | None = None
        self.goal_x: float | None = None
        self.buffer: Buffer = buffer
        self.cursor: Cursor = buffer.cursor(0)
        self.anchor: Marker = buffer.marker(0)
        self._held: tuple[int, int] | None = None  # lines the frontend holds, inclusive
        # Changed line ranges not yet sent: (first line now, lines before, lines now),
        # sorted and disjoint.
        self._dirty: list[tuple[int, int, int]] = []
        self._drop_held = False
        self._sent_info: dict[str, Any] | None = None
        self._sent_selections: Any = None
        self._sent_modeline: Any = None
        self._reveal: tuple[int, int, str] | None = (1, 0, "top")
        buffer.add_listener(self._on_change)
        frame.views[self.id] = self

    # -- what is shown ------------------------------------------------------

    @provisional("0.1")
    def show(self, buffer: Buffer) -> None:
        """Display BUFFER in this view."""
        if buffer is self.buffer:
            return
        self.buffer.remove_listener(self._on_change)
        self.cursor.detach()
        self.anchor.detach()
        self.buffer = buffer
        self.cursor = buffer.cursor(0)
        self.anchor = buffer.marker(0)
        buffer.add_listener(self._on_change)
        # The frontend will be at the top of the new buffer.  It reports its
        # viewport only when that changes, so assume the page it had.
        if self.viewport is not None:
            page = self.viewport[2]
            self.viewport = (1, page, page)
        self.goal_x = None
        self._dirty = []
        self._drop_held = self._held is not None
        self._held = None
        self._reveal = (1, 0, "top")
        self.frame.schedule_update()

    @provisional("0.1")
    def reveal(self, pos: int | None = None, align: str = "nearest") -> None:
        """Ask the frontend to scroll so POS (default: the cursor) is visible."""
        line, offset = self.buffer.line_offset(self.cursor.pos if pos is None else pos)
        self._reveal = (line, offset, align)
        self.frame.schedule_update()

    @provisional("0.1")
    def collapse(self) -> None:
        """Make the selection empty, at the cursor."""
        self.anchor.pos = self.cursor.pos

    def close(self) -> None:
        self.buffer.remove_listener(self._on_change)
        self.cursor.detach()
        self.anchor.detach()
        self.frame.views.pop(self.id, None)

    def _on_change(self, change: Change) -> None:
        self._dirty = merge_dirty(self._dirty, *change.lines)
        self.frame.schedule_update()

    # -- geometry -----------------------------------------------------------

    @provisional("0.1")
    async def move_visual(self, count: int, unit: str = "visual_line") -> None:
        """Move the cursor COUNT visual rows or pages, as the frontend lays them out."""
        buf = self.buffer
        line, offset = buf.line_offset(self.cursor.pos)
        try:
            reply = await self.frame.request(
                "layout.move", self.id, [[line, offset, self.goal_x]], unit, count
            )
            new_line, new_offset, goal = reply[0]
            new_line = max(1, min(buf.line_count, int(new_line)))
            pos = buf.position(new_line, int(new_offset))
        except RpcError, FrontendGone, NoFrame, TimeoutError, LookupError, TypeError, ValueError:
            # No usable answer: fall back to logical lines.
            page = self.viewport[2] if self.viewport else 20
            self.cursor.forward_line(count * (page if unit == "page" else 1))
            self.goal_x = None
            return
        self.cursor.pos = pos if buf.is_boundary(pos) else buf.prev_boundary(pos)
        self.goal_x = goal

    @provisional("0.1")
    async def visual_line_edge(self, which: str) -> int:
        """The position at the start or end of the visual row the cursor is on."""
        buf = self.buffer
        line, offset = buf.line_offset(self.cursor.pos)
        try:
            reply = await self.frame.request("layout.line_edge", self.id, line, offset, which)
            return buf.position(int(reply[0]), int(reply[1]))
        except RpcError, FrontendGone, NoFrame, TimeoutError, LookupError, TypeError, ValueError:
            return buf.line_start(line) if which == "start" else buf.line_end(line)

    # -- what the frontend is told ------------------------------------------

    def info(self) -> dict[str, Any]:
        options = self.buffer.locals
        return {
            "name": self.buffer.name,
            "line_count": self.buffer.line_count,
            "wrap": self.wrap,
            "gutter": options.get("gutter_columns", [{"id": "numbers", "kind": "numbers",
                                                      "side": "left", "mode": "absolute"}]),
            "margins": options.get("margins", {"left": 0, "right": 0}),
            "max_width": options.get("max_width"),
            "tab_width": options.get("tab_width", 8),
        }  # fmt: skip

    def selections(self) -> list[list[int]]:
        buf = self.buffer
        return [[*buf.line_offset(self.anchor.pos), *buf.line_offset(self.cursor.pos)]]

    def modeline(self) -> list[list[Any]] | None:
        return None

    def reset_sync(self) -> None:
        """Forget what the frontend has: it is about to be told everything again."""
        top = self.viewport[0] if self.viewport else 1
        self._held = None
        self._dirty = []
        self._drop_held = False
        self._sent_info = None
        self._sent_selections = None
        self._sent_modeline = None
        self._reveal = (min(top, self.buffer.line_count), 0, "top")

    def _lines(self, first: int, last: int) -> list[Any]:
        faces = self.frame.editor.faces
        providers = self.buffer.providers
        built: list[Any] = []
        for number in range(first, last + 1):
            line = build_line(self.buffer, number, faces, providers)
            for run in line[1]:
                self.frame.use_face(run[2])
            for mark in line[0].get("gutter", {}).values():
                self.frame.use_face(mark[1])
            built.append(line)
        return built

    def sync(self, events: list[Any]) -> None:
        """Append the events that bring the frontend's copy of this view up to date."""
        buf = self.buffer
        count = buf.line_count
        info = self.info()
        if info != self._sent_info:
            events.append(["win_info", self.id, info])
            self._sent_info = info
        if self._drop_held:
            events.append(["held", self.id, 1, 0])
            self._drop_held = False

        # Send the changed ranges from the bottom up, so the line numbers of
        # those still to come are the ones the frontend has.
        ranges, self._dirty = self._dirty, []
        shift = sum(new - old for _, old, new in ranges)
        for now, old, new in reversed(ranges):
            shift -= new - old
            self._send_change(events, now - shift, old, new, now)

        self._fill(events, count)

        selections = [self.selections(), self.shape]
        if selections != self._sent_selections:
            events.append(["selections", self.id, 0, selections[0], self.shape])
            self._sent_selections = selections
        modeline = self.modeline()
        if modeline != self._sent_modeline:
            if modeline is not None:
                for run in modeline:
                    self.frame.use_face(run[2])
                events.append(["modeline", self.id, modeline])
            self._sent_modeline = modeline
        if self._reveal is not None:
            line, offset, align = self._reveal
            events.append(["reveal", self.id, min(line, count), offset, align])
            self._reveal = None

    def _send_change(self, events: list[Any], first: int, old: int, new: int, now: int) -> None:
        """OLD lines at FIRST, in the frontend's numbering, became NEW lines, which
        are at NOW in the buffer."""
        if self.viewport is not None and first + old <= self.viewport[0] and self.viewport[0] > 1:
            # Lines came or went above what is on screen.  The frontend keeps
            # the same text in view, so its viewport moves by the difference.
            # (A viewport at the very top stays there instead.)
            v1, v2, page = self.viewport
            self.viewport = (v1 + new - old, v2 + new - old, page)
        if self._held is None:
            return
        h1, h2 = self._held
        if first + old <= h1:  # wholly above what is held: just renumber
            events.append(["lines", self.id, first, old, new])
            self._held = (h1 + new - old, h2 + new - old)
        elif first > h2:  # wholly below
            events.append(["lines", self.id, first, old, new])
        elif new > SPLICE_LIMIT:
            # Too much to send unasked.  Keep only what is above it.
            self._held = (h1, first - 1) if first > h1 else None
            events.append(
                ["held", self.id, h1 if first > h1 else 1, first - 1 if first > h1 else 0]
            )
            events.append(["lines", self.id, first, old, new])
        else:
            events.append(["lines", self.id, first, old, self._lines(now, now + new - 1)])
            before = max(0, first - h1)
            after = max(0, h2 - (first + old) + 1)
            start = min(h1, first)
            total = before + new + after
            self._held = (start, start + total - 1) if total > 0 else None

    def _fill(self, events: list[Any], count: int) -> None:
        """Send the lines around the viewport that the frontend does not hold yet."""
        if self.viewport is None:
            return
        v1, v2, page = self.viewport
        margin = max(2 * page, 40)
        want1 = max(1, min(v1, count) - margin)
        want2 = min(count, max(v2, v1) + margin)
        if want2 < want1:
            return
        held = self._held
        if held is not None and (want1 > held[1] + 1 or want2 < held[0] - 1):
            events.append(["held", self.id, 1, 0])  # nowhere near: start again
            held = None
        if held is None:
            events.append(["lines", self.id, want1, want2 - want1 + 1, self._lines(want1, want2)])
            self._held = (want1, want2)
            return
        h1, h2 = held
        if want1 < h1:
            events.append(["lines", self.id, want1, h1 - want1, self._lines(want1, h1 - 1)])
            h1 = want1
        if want2 > h2:
            events.append(["lines", self.id, h2 + 1, want2 - h2, self._lines(h2 + 1, want2)])
            h2 = want2
        span = want2 - want1 + 1
        if h2 - h1 + 1 > 4 * span:  # holding far more than is useful: trim
            h1, h2 = max(h1, want1 - span), min(h2, want2 + span)
            events.append(["held", self.id, h1, h2])
        self._held = (h1, h2)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id} on {self.buffer.name!r}>"


@provisional("0.1")
class Window(View):
    """A view in the frame's window tree.  It has a modeline."""

    def modeline(self) -> list[list[Any]] | None:
        buf = self.buffer
        line, _ = buf.line_offset(self.cursor.pos)
        state = "*" if buf.modified else "-"
        text = f" {state} {buf.name}   L{line} C{self.cursor.column} "
        return [[None, text, self.frame.editor.faces.id_for("modeline")]]


@provisional("0.1")
class Frame:
    """One attached frontend: its windows, and the pacing of updates to it (ENGINE 4.8)."""

    def __init__(self, editor: Editor, conn: Connection, capabilities: list[str]) -> None:
        self.id = next(_frame_ids)
        self.token = secrets.token_hex(16)
        self.editor = editor
        self.conn: Connection | None = conn
        self.capabilities = list(capabilities)
        self.views: dict[int, View] = {}
        self.title = "Neomacs"
        self.stalled = False
        self._faces_sent: set[int] = set()
        self._new_faces: list[int] = []
        self._faces_generation = -1
        self._seq = 0
        self._in_flight: set[int] = set()
        self._scheduled = False
        self._pending_reset = True
        self._sent_layout: Any = None
        self._sent_title: str | None = None
        self._sent_focus: int | None = None
        self._stall_timer: asyncio.TimerHandle | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        scratch = editor.buffers.get("*scratch*")
        if scratch is None:  # an empty buffer is falsy, so test for None
            scratch = editor.buffers.new("*scratch*")
        self.window: Window = Window(self, scratch)
        self.focus: View = self.window
        editor.frames[self.id] = self
        self.schedule_update()

    @property
    def attached(self) -> bool:
        return self.conn is not None and not self.conn.closed

    @property
    def busy(self) -> bool:
        """True while an update is on its way out or awaiting acknowledgement."""
        return self.attached and not self.stalled and (self._scheduled or bool(self._in_flight))

    # -- updates ------------------------------------------------------------

    @provisional("0.1")
    def schedule_update(self) -> None:
        """Bring the frontend up to date soon, pacing permitting."""
        if self._scheduled or not self.attached:
            return
        loop = self.editor.loop
        if loop is None:
            return
        self._scheduled = True
        loop.call_soon(self._update)

    def _update(self, force: bool = False) -> None:
        self._scheduled = False
        if not self.attached or (self._in_flight and not force):
            return  # the acknowledgement will bring us back here
        events = self.editor.guard(f"building an update for frame {self.id}", self._collect)
        if not events:
            return
        conn = self.conn
        assert conn is not None
        self._seq += 1
        events.append(["flush", self._seq])
        self._in_flight.add(self._seq)
        conn.notify("redraw", events)
        loop = self.editor.loop
        assert loop is not None
        if self._stall_timer is None:
            self._stall_timer = loop.call_later(STALL_SECONDS, self._stall)

    def use_face(self, face_id: int) -> None:
        if face_id not in self._faces_sent:
            self._faces_sent.add(face_id)
            self._new_faces.append(face_id)

    def _collect(self) -> list[Any]:
        faces = self.editor.faces
        prefix: list[Any] = []
        body: list[Any] = []
        if self._pending_reset:
            self._pending_reset = False
            prefix.append(["reset"])
            self._faces_sent.clear()
            self._sent_layout = self._sent_title = self._sent_focus = None
            self._faces_generation = -1
            for view in self.views.values():
                view.reset_sync()
        ui_faces: dict[str, int] | None = None
        if self._faces_generation != faces.generation:
            self._faces_generation = faces.generation
            self._new_faces = sorted(self._faces_sent | {0})
            self._faces_sent.add(0)
            ui_faces = {name: faces.id_for(name) for name in UI_FACES}
            for face_id in ui_faces.values():
                self.use_face(face_id)
        layout = {"main": ["win", self.window.id], "panels": []}
        if layout != self._sent_layout:
            body.append(["layout", layout])
            self._sent_layout = layout
        if self.title != self._sent_title:
            body.append(["title", self.title])
            self._sent_title = self.title
        for view in list(self.views.values()):
            view.sync(body)
        if self.focus.id != self._sent_focus:
            body.append(["focus", self.focus.id])
            self._sent_focus = self.focus.id
        for face_id in self._new_faces:
            prefix.append(["face_define", face_id, faces.attrs(face_id)])
        self._new_faces = []
        if ui_faces is not None:
            prefix.append(["ui_faces", ui_faces])
        return prefix + body

    def ack(self, seq: int) -> None:
        self._in_flight.discard(seq)
        if not self._in_flight:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
                self._stall_timer = None
            if self.stalled:
                self.stalled = False
                self.editor.message(f"Frame {self.id} is responding again")
            self.schedule_update()

    def _stall(self) -> None:
        self._stall_timer = None
        if self._in_flight and self.attached:
            self.stalled = True
            self.editor.message(f"Frame {self.id} has stopped acknowledging updates")

    @provisional("0.1")
    def full_sync(self) -> None:
        """Send the frontend everything again, starting with `reset` (ENGINE 4.6)."""
        self._pending_reset = True
        self._in_flight.clear()
        self.schedule_update()

    @provisional("0.1")
    async def request(self, method: str, *params: Any, timeout: float = GEOMETRY_TIMEOUT) -> Any:
        """Ask the frontend something.  It is first sent any pending update,
        so it answers against the text the engine has."""
        if not self.attached:
            raise NoFrame(f"frame {self.id} has no frontend attached")
        self._update(force=True)
        conn = self.conn
        assert conn is not None
        return await conn.request(method, *params, timeout=timeout)

    # -- attachment ---------------------------------------------------------

    def detach(self) -> None:
        """The connection has gone.  Keep the frame so a frontend can come back to it."""
        self.conn = None
        self._in_flight.clear()
        self._scheduled = False
        if self._stall_timer is not None:
            self._stall_timer.cancel()
            self._stall_timer = None
        loop = self.editor.loop
        if loop is not None and self.id in self.editor.frames:
            self._expiry_timer = loop.call_later(self.editor.frame_detach_timeout, self.delete)

    def reattach(self, conn: Connection, capabilities: list[str]) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        self.conn = conn
        self.capabilities = list(capabilities)
        self.stalled = False
        self.full_sync()

    @provisional("0.1")
    def delete(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        for view in list(self.views.values()):
            view.close()
        self.editor.frames.pop(self.id, None)
        self.conn = None

    def __repr__(self) -> str:
        state = "attached" if self.attached else "detached"
        return f"<Frame {self.id} {state}>"
