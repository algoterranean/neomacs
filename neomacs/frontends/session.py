"""A frontend's connection: applies `redraw`, lays out, reports viewports, answers requests.

Shared by the terminal frontend and the headless test client.  Everything
that arrives from the engine is handled on the client's reader thread, in
order, under `lock`; a geometry request is therefore always answered against
the redraw that preceded it.

The screen, top to bottom: the window's text, its modeline, and one or more
rows for the prompt or the latest message.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from neomacs.frontends.model import FrameModel, ViewModel
from neomacs.frontends.monolayout import MonoView
from neomacs.frontends.screen import MIN_ROWS, Arrangement, Rect, arrange, tab_line
from neomacs.rpc.api import PROTOCOL
from neomacs.testing.client import Client, NotReady


class Session:
    def __init__(
        self,
        client: Client,
        rows: int = 24,
        cols: int = 80,
        *,
        auto_ack: bool = True,
        attach: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.rows = rows
        self.cols = cols
        self.auto_ack = auto_ack
        self.lock = threading.RLock()
        self.model = FrameModel()
        self.layouts: dict[int, MonoView] = {}
        self.unacked: list[int] = []
        self.batches = 0
        self.clipboard = ""  # stands in for the system clipboard
        self.open_frame_requests = 0
        self.on_redraw: Callable[[], None] | None = None
        self._reported: dict[int, tuple[int, int, int]] = {}
        self.arrangement = Arrangement()
        self.placed: dict[int, Rect] = {}
        client.on_notification = self._notified
        client.request_handlers.update(
            {
                "layout.move": self._move,
                "layout.line_edge": self._line_edge,
                "layout.viewport": self._viewport,
                "layout.rect": self._rect,
                "clipboard.get": self._clipboard_get,
            }
        )
        options: dict[str, Any] = {"protocol": PROTOCOL, "capabilities": ["panels"]}
        options.update(attach or {})
        reply = client.call("ui.attach", options)
        self.frame_id: int = reply["frame"]
        self.token: str = reply["token"]

    # -- size ---------------------------------------------------------------

    def prompt_label(self) -> str:
        prompt = self.model.prompt
        if prompt is None:
            return ""
        return "".join(str(run[1]) for run in prompt[1].get("label", []))

    @property
    def prompt_rows(self) -> int:
        """Rows at the bottom for the prompt or a message: one, or more for a multi-line prompt."""
        prompt = self.model.prompt
        if prompt is None or not prompt[1].get("multiline"):
            return 1
        view, options = prompt
        low, high = int(options.get("min_lines", 1)), int(options.get("max_lines", 10))
        return max(1, min(max(low, view.line_count), high, self.rows - 3))

    @property
    def main_rows(self) -> int:
        """Rows above the prompt: the main area and the panels."""
        return max(MIN_ROWS, self.rows - self.prompt_rows)

    @property
    def text_rows(self) -> int:
        """Rows of text in the main view."""
        view = self.model.main_view
        rect = self.arrangement.views.get(view.id) if view is not None else None
        return rect.h if rect is not None else max(1, self.main_rows - 1)

    def _cursor_cell(self, view_id: Any, line: Any, offset: Any) -> tuple[int, int] | None:
        """Where a position is on screen, for a panel that floats beside it."""
        rect = self.placed.get(view_id)
        layout = self.layouts.get(view_id)
        if rect is None or layout is None or line is None:
            return None
        cell = layout.cursor_cell(int(line), int(offset or 0))
        return (rect.y + cell[0], rect.x + cell[1]) if cell is not None else None

    def _arrange(self) -> None:
        self.placed = self.arrangement.views
        self.arrangement = arrange(self.model.layout, self.cols, self.main_rows, self._cursor_cell)
        self.placed = self.arrangement.views

    def _size(self, view: ViewModel) -> tuple[int, int]:
        prompt = self.model.prompt
        if prompt is not None and prompt[0] is view:
            return max(1, self.cols - len(self.prompt_label())), self.prompt_rows
        rect = self.arrangement.views.get(view.id)
        if rect is not None:
            return max(1, rect.w), max(1, rect.h)
        # Not on screen: an inactive tab, a hidden panel, an inline minibuffer.
        return max(1, self.cols // 2), 1

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            self.rows, self.cols = rows, cols
            self._relayout()

    # -- from the engine ----------------------------------------------------

    def _notified(self, method: str, params: list[Any]) -> None:
        if method == "clipboard.set":
            self.clipboard = str(params[1].get("text", ""))
            return
        if method == "ui.open_frame":
            self.open_frame_requests += 1
            return
        if method != "redraw":
            return
        with self.lock:
            flushed = self.model.apply(params[0])
            self.batches += 1
            self._relayout()
            self.client.retry_deferred()  # a question may have been waiting for these lines
            if self.auto_ack:
                for seq in flushed:
                    self.client.notify("view.ack", seq)
            else:
                self.unacked.extend(flushed)
        if self.on_redraw is not None:
            self.on_redraw()

    def _relayout(self) -> None:
        self._arrange()
        for view_id in list(self.layouts):
            if view_id not in self.model.views:
                del self.layouts[view_id]
                self._reported.pop(view_id, None)
        for view in self.model.views.values():
            layout = self.layout(view)
            size = self._size(view)
            if (layout.width, layout.height) != size:
                layout.resize(*size)
            layout.apply_pending_reveal()
        self._report_viewports()

    def layout(self, view: ViewModel) -> MonoView:
        layout = self.layouts.get(view.id)
        if layout is None or layout.model is not view:
            layout = self.layouts[view.id] = MonoView(view, *self._size(view))
        return layout

    def _report_viewports(self) -> None:
        for view in self.model.views.values():
            viewport = self.layout(view).viewport()
            if self._reported.get(view.id) != viewport:
                self._reported[view.id] = viewport
                self.client.notify("view.viewport", view.id, *viewport)

    def ack(self) -> None:
        """Acknowledge every batch received so far (when not acknowledging automatically)."""
        with self.lock:
            pending, self.unacked = self.unacked, []
        for seq in pending:
            self.client.notify("view.ack", seq)

    def scroll(self, rows: int) -> None:
        """Scroll the main view, as a wheel would."""
        with self.lock:
            view = self.model.main_view
            if view is not None:
                self.layout(view).scroll_rows(rows)
                self._report_viewports()

    @property
    def focused(self) -> ViewModel | None:
        focus = self.model.focus
        return self.model.views.get(focus) if focus is not None else self.model.main_view

    def compose(self) -> list[list[tuple[str, int]]]:
        """The screen above the prompt rows, as rows of (text, face id) cells.

        A face id below zero is one of the frontend's own: -1 for the active
        tab, -2 for other tabs and rules.  A wide character is followed by a
        cell with no text.
        """
        with self.lock:
            cols, rows = self.cols, self.main_rows
            grid: list[list[tuple[str, int]]] = [[(" ", 0)] * cols for _ in range(rows)]

            def put(x: int, y: int, text: str, face: int, limit: int) -> int:
                for ch in text:
                    if x >= limit or not 0 <= y < rows or not 0 <= x < cols:
                        break
                    grid[y][x] = (ch, face)
                    x += 1
                return x

            def draw_view(view_id: int, rect: Rect) -> None:
                view = self.model.views.get(view_id)
                if view is None:
                    return
                layout = self.layout(view)
                gutter_face = self.model.ui_faces.get("gutter", 0)
                for dy, row in enumerate(layout.visible()[: rect.h]):
                    y = rect.y + dy
                    for x in range(rect.x, min(cols, rect.x + rect.w)):
                        if 0 <= y < rows:
                            grid[y][x] = (" ", 0)
                    if row is None:
                        continue
                    x = put(rect.x, y, layout.gutter_text(row), gutter_face, rect.x + rect.w)
                    for cell in row.cells:
                        if x + cell.width > rect.x + rect.w or not 0 <= y < rows:
                            break
                        if cell.width > 0 and x < cols:
                            grid[y][x] = (cell.text, cell.face)
                            for extra in range(1, cell.width):
                                if x + extra < cols:
                                    grid[y][x + extra] = ("", cell.face)
                            x += cell.width

            arrangement = self.arrangement
            floating = {view_id for _, ids in arrangement.boxes for view_id in ids}
            for view_id, rect in arrangement.views.items():
                if view_id not in floating:
                    draw_view(view_id, rect)
            for view_id, x, y, width in arrangement.modelines:
                view = self.model.views.get(view_id)
                runs = (view.modeline if view is not None else None) or []
                text = "".join(str(run[1]) for run in runs)[:width].ljust(width)
                face = runs[0][2] if runs else self.model.ui_faces.get("modeline", 0)
                put(x, y, text, face, x + width)
            for x, y, height in arrangement.rules:
                for dy in range(height):
                    put(x, y + dy, "│", -2, x + 1)
            boxed = {
                id(panel) for panel, *_ in arrangement.tablines if panel.get("mode") != "docked"
            }
            for panel, x, y, width in arrangement.tablines:
                if id(panel) in boxed:
                    continue
                for text, active in tab_line(panel, width):
                    x = put(x, y, text, -1 if active else -2, x + len(text))
            for box, view_ids in arrangement.boxes:
                for dy in range(box.h):
                    y = box.y + dy
                    if dy == 0:
                        line = "┌" + "─" * (box.w - 2) + "┐"
                    elif dy == box.h - 1:
                        line = "└" + "─" * (box.w - 2) + "┘"
                    else:
                        line = "│" + " " * (box.w - 2) + "│"
                    put(box.x, y, line, -2, box.x + box.w)
                for panel, x, y, width in arrangement.tablines:
                    if id(panel) in boxed and panel.get("active") in view_ids:
                        for text, active in tab_line(panel, width):
                            x = put(x, y, text, -1 if active else -2, x + len(text))
                for view_id in view_ids:
                    draw_view(view_id, arrangement.views[view_id])
            return grid

    def cursor_position(self) -> tuple[int, int] | None:
        """(row, column) of the focused view's cursor on screen, if it is in view."""
        with self.lock:
            focused = self.focused
            if focused is None or not focused.selections:
                return None
            sel = focused.selections[focused.primary]
            cell = self.layout(focused).cursor_cell(sel[2], sel[3])
            if cell is None:
                return None
            prompt = self.model.prompt
            if prompt is not None and focused is prompt[0]:
                return self.main_rows + cell[0], len(self.prompt_label()) + cell[1]
            rect = self.arrangement.views.get(focused.id)
            if rect is None or cell[0] >= rect.h:
                return None
            return rect.y + cell[0], rect.x + cell[1]

    def bottom_text(self) -> str:
        """What the message row shows when there is no prompt."""
        text = self.model.message[0] if self.model.message else ""
        keys = self.model.keys
        return keys if keys else text

    # -- requests from the engine -------------------------------------------

    def _mono(self, view_id: int) -> MonoView:
        return self.layout(self.model.view(view_id))

    def _answer[T](self, view_id: int, compute: Callable[[MonoView], T]) -> T:
        """Answer a geometry request, but only from lines that are held.

        If the answer depended on a line this frontend does not hold, ask the
        engine for it and raise NotReady; the request is tried again when the
        next batch has been applied (FRONTEND 7).
        """
        with self.lock:
            mono = self._mono(view_id)
            model = mono.model
            top = (model.top_line, model.top_row)
            mono.missing.clear()
            result = compute(mono)
            if mono.missing:
                model.top_line, model.top_row = top  # undo any scrolling: it will be redone
                first, last = min(mono.missing), max(mono.missing)
                mono.missing.clear()
                self._reported.pop(view_id, None)  # the real viewport is reported again after
                self.client.notify("view.viewport", view_id, first, last, mono.height)
                raise NotReady(f"lines {first} to {last}")
            self._report_viewports()
            return result

    def _move(self, view: int, positions: list[list[Any]], unit: str, count: int) -> Any:
        return self._answer(view, lambda mono: mono.move(positions, unit, count))

    def _line_edge(self, view: int, line: int, offset: int, which: str) -> Any:
        return self._answer(view, lambda mono: mono.line_edge(line, offset, which))

    def _viewport(self, view: int) -> Any:
        with self.lock:
            first, last, page = self._mono(view).viewport()
            return {"first": first, "last": last, "page_lines": page}

    def _rect(self, view: int, line: int, offset: int) -> Any:
        return self._answer(view, lambda mono: mono.rect(line, offset))

    def _clipboard_get(self, which: str, formats: list[str]) -> Any:
        return {"text": self.clipboard}
