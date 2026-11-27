"""A frontend's connection: applies `redraw`, lays out, reports viewports, answers geometry.

Shared by the terminal frontend and the headless test client.  Everything
that arrives from the engine is handled on the client's reader thread, in
order, under `lock`; a geometry request is therefore always answered against
the redraw that preceded it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from neomacs.frontends.model import FrameModel, ViewModel
from neomacs.frontends.monolayout import MonoView
from neomacs.rpc.api import PROTOCOL
from neomacs.testing.client import Client


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
        self.on_redraw: Callable[[], None] | None = None
        self._reported: dict[int, tuple[int, int, int]] = {}
        client.on_notification = self._notified
        client.request_handlers.update(
            {
                "layout.move": self._move,
                "layout.line_edge": self._line_edge,
                "layout.viewport": self._viewport,
                "layout.rect": self._rect,
            }
        )
        options: dict[str, Any] = {"protocol": PROTOCOL, "capabilities": []}
        options.update(attach or {})
        reply = client.call("ui.attach", options)
        self.frame_id: int = reply["frame"]
        self.token: str = reply["token"]

    # -- size ---------------------------------------------------------------

    @property
    def text_rows(self) -> int:
        """Rows for the window's text: everything but the modeline."""
        return max(1, self.rows - 1)

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            self.rows, self.cols = rows, cols
            for layout in self.layouts.values():
                layout.resize(cols, self.text_rows)
            self._report_viewports()

    # -- from the engine ----------------------------------------------------

    def _notified(self, method: str, params: list[Any]) -> None:
        if method != "redraw":
            return
        with self.lock:
            flushed = self.model.apply(params[0])
            self.batches += 1
            for view_id in list(self.layouts):
                if view_id not in self.model.views:
                    del self.layouts[view_id]
                    self._reported.pop(view_id, None)
            for view in self.model.views.values():
                self.layout(view).apply_pending_reveal()
            self._report_viewports()
            if self.auto_ack:
                for seq in flushed:
                    self.client.notify("view.ack", seq)
            else:
                self.unacked.extend(flushed)
        if self.on_redraw is not None:
            self.on_redraw()

    def layout(self, view: ViewModel) -> MonoView:
        layout = self.layouts.get(view.id)
        if layout is None or layout.model is not view:
            layout = self.layouts[view.id] = MonoView(view, self.cols, self.text_rows)
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

    # -- geometry requests --------------------------------------------------

    def _mono(self, view_id: int) -> MonoView:
        return self.layout(self.model.view(view_id))

    def _move(self, view: int, positions: list[list[Any]], unit: str, count: int) -> Any:
        with self.lock:
            result = self._mono(view).move(positions, unit, count)
            self._report_viewports()
            return result

    def _line_edge(self, view: int, line: int, offset: int, which: str) -> Any:
        with self.lock:
            return self._mono(view).line_edge(line, offset, which)

    def _viewport(self, view: int) -> Any:
        with self.lock:
            first, last, page = self._mono(view).viewport()
            return {"first": first, "last": last, "page_lines": page}

    def _rect(self, view: int, line: int, offset: int) -> Any:
        with self.lock:
            return self._mono(view).rect(line, offset)
