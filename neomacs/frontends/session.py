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
        options: dict[str, Any] = {"protocol": PROTOCOL, "capabilities": []}
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
    def text_rows(self) -> int:
        """Rows for the window's text: everything but the modeline and the prompt rows."""
        return max(1, self.rows - 1 - self.prompt_rows)

    def _size(self, view: ViewModel) -> tuple[int, int]:
        prompt = self.model.prompt
        if prompt is not None and prompt[0] is view:
            return max(1, self.cols - len(self.prompt_label())), self.prompt_rows
        if view.id in self.model.minibuffers or view is not self.model.main_view:
            return max(1, self.cols // 2), 1
        return self.cols, self.text_rows

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
