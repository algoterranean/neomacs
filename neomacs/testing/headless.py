"""A frontend with no display, for tests: it holds what a real one would and can say
what would be on screen."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from neomacs.frontends.model import ViewModel
from neomacs.frontends.monolayout import MonoView
from neomacs.frontends.session import Session
from neomacs.testing.client import Client


class HeadlessUI(Session):
    def __init__(
        self,
        path: Path | str,
        rows: int = 24,
        cols: int = 80,
        *,
        auto_ack: bool = True,
        attach: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(Client(path), rows, cols, auto_ack=auto_ack, attach=attach)
        if auto_ack:
            self.idle()

    # -- driving the engine -------------------------------------------------

    def idle(self, timeout: float = 10.0) -> None:
        """Wait until the engine has handled all input and this frontend is up to date."""
        if not self.client.call("engine.idle", timeout, timeout=timeout + 5):
            raise TimeoutError("the engine did not become idle")

    def keys(self, *keys: str) -> None:
        for key in keys:
            self.client.notify("input.key", key)
        self.idle()

    def type(self, text: str) -> None:
        """Type TEXT one key at a time."""
        names = {" ": "<space>", "\n": "<return>", "\t": "<tab>"}
        self.keys(*(names.get(ch, ch) for ch in text))

    def eval(self, source: str) -> Any:
        value = self.client.eval(source)
        self.idle()
        return value

    def open(self, path: Path | str) -> None:
        self.eval(f"ed.frames[{self.frame_id}].window.show(ed.buffers.visit({str(path)!r}))")

    # -- looking ------------------------------------------------------------

    @property
    def view(self) -> ViewModel:
        view = self.model.main_view
        assert view is not None, "no layout received yet"
        return view

    @property
    def mono(self) -> MonoView:
        return self.layout(self.view)

    def screen(self) -> list[str]:
        """The text rows as displayed, then the modeline."""
        with self.lock:
            rows = self.mono.render()
            modeline = "".join(run[1] for run in (self.view.modeline or []))
            return [*rows, modeline]

    def text_rows_shown(self) -> list[str]:
        """The text rows without trailing empty ones."""
        rows = self.screen()[:-1]
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def cursor(self) -> tuple[int, int] | None:
        """(row, column) of the primary cursor on screen."""
        with self.lock:
            sel = self.view.selections[self.view.primary]
            return self.mono.cursor_cell(sel[2], sel[3])

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HeadlessUI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
