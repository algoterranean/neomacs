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
        self.call("files.find", str(path))

    # -- looking ------------------------------------------------------------

    @property
    def view(self) -> ViewModel:
        view = self.model.main_view
        assert view is not None, "no layout received yet"
        return view

    @property
    def mono(self) -> MonoView:
        return self.layout(self.view)

    def bottom_rows(self) -> list[str]:
        """The rows under the modeline: the prompt with what has been typed, or a message."""
        with self.lock:
            prompt = self.model.prompt
            if prompt is None:
                return [self.bottom_text()]
            rows = self.layout(prompt[0]).render()
            label = self.prompt_label()
            return [(label if i == 0 else " " * len(label)) + row for i, row in enumerate(rows)]

    def screen(self) -> list[str]:
        """The text rows as displayed, the modeline, then the prompt or message rows."""
        with self.lock:
            rows = self.mono.render()
            modeline = "".join(run[1] for run in (self.view.modeline or []))
            return [*rows, modeline, *self.bottom_rows()]

    def grid(self) -> list[str]:
        """Everything above the prompt rows as it would be drawn: windows with
        their modelines, panels with their tab lines, floating boxes."""
        return ["".join(text for text, _ in row).rstrip() for row in self.compose()]

    def view_showing(self, title: str) -> ViewModel | None:
        """The view whose buffer is called TITLE, if one is on show."""
        with self.lock:
            for view in self.model.views.values():
                if view.info.get("name") == title:
                    return view
        return None

    def panels(self) -> dict[str, dict[str, Any]]:
        """The frame's panels by name, as the last layout described them."""
        layout = self.model.layout or {}
        return {panel["name"]: panel for panel in layout.get("panels", [])}

    def lines_of(self, view: ViewModel) -> list[str]:
        """The text of the lines a view holds."""
        return ["".join(str(run[1]) for run in line[1] if len(run) == 3) for line in view.lines]

    def text_rows_shown(self) -> list[str]:
        """The text rows without trailing empty ones."""
        with self.lock:
            rows = self.mono.render()
        while rows and not rows[-1]:
            rows.pop()
        return rows

    @property
    def modeline(self) -> str:
        """The modeline's text, without what is at its right-hand end."""
        return "".join(run[1] for run in (self.view.modeline or []))

    @property
    def modeline_right(self) -> str:
        """The text at the right-hand end of the modeline."""
        return "".join(run[1] for run in self.view.modeline_right)

    @property
    def message(self) -> str:
        """The latest message from the engine."""
        return self.model.message[0] if self.model.message else ""

    @property
    def prompt(self) -> str | None:
        """The prompt's label and input, or None when nothing is being asked."""
        with self.lock:
            return self.bottom_rows()[0] if self.model.prompt is not None else None

    def candidates(self) -> list[str]:
        """The completion candidates on show."""
        popup = self.model.popup
        return [item[0] for item in popup["items"]] if popup else []

    def cursors(self) -> list[tuple[int, int]]:
        """(line, offset) of every cursor in the main view, in buffer order."""
        return [(s[2], s[3]) for s in self.view.selections]

    def cursor(self) -> tuple[int, int] | None:
        """(row, column) of the main view's primary cursor on screen."""
        with self.lock:
            sel = self.view.selections[self.view.primary]
            return self.mono.cursor_cell(sel[2], sel[3])

    def call(self, command: str, *args: Any) -> Any:
        """Run a command by name and wait for the display to settle."""
        value = self.client.call("engine.call", command, list(args))
        self.idle()
        return value

    def mouse(
        self, line: int, offset: int, *, button: int = 1, action: str = "press",
        clicks: int = 1, mods: str = "", area: str = "text",
    ) -> None:  # fmt: skip
        self.client.notify(
            "input.mouse", button, action, clicks, mods, self.view.id, area, line, offset
        )
        self.idle()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HeadlessUI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
