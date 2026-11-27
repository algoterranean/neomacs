"""What a frontend holds: the result of applying `redraw` events (ENGINE 4.3).

This is the reference for how each event changes a frontend's state.  The
terminal frontend and the headless test client use it directly.
"""

from __future__ import annotations

from typing import Any

Line = list[Any]  # [attrs, runs]


class ProtocolError(Exception):
    """The engine sent something a frontend cannot apply."""


class ViewModel:
    def __init__(self, view_id: int) -> None:
        self.id = view_id
        self.info: dict[str, Any] = {"line_count": 1, "wrap": True, "tab_width": 8, "gutter": []}
        self.first = 1  # line number of lines[0]
        self.lines: list[Line] = []
        self.selections: list[list[int]] = []
        self.primary = 0
        self.shape = "block"
        self.modeline: list[Any] | None = None
        # Scroll position, kept on the same text across splices.
        self.top_line = 1
        self.top_row = 0
        self.pending_reveal: tuple[int, int, str] | None = None

    @property
    def line_count(self) -> int:
        return int(self.info.get("line_count", 1))

    @property
    def held(self) -> tuple[int, int] | None:
        return (self.first, self.first + len(self.lines) - 1) if self.lines else None

    def line(self, number: int) -> Line | None:
        index = number - self.first
        return self.lines[index] if 0 <= index < len(self.lines) else None

    def hold(self, first: int, last: int) -> None:
        """Keep only lines FIRST..LAST of what is held."""
        if not self.lines or first > last:
            self.lines = []
            return
        lo = max(first, self.first)
        hi = min(last, self.first + len(self.lines) - 1)
        self.lines = self.lines[lo - self.first : hi - self.first + 1] if lo <= hi else []
        self.first = lo

    def splice(self, first: int, removed: int, payload: list[Line] | int) -> None:
        """Replace REMOVED lines starting at FIRST with PAYLOAD (lines, or a count of lines)."""
        if isinstance(payload, int):
            new = payload
            # Never touches held lines: either wholly above them, or below.
            if self.lines and first + removed <= self.first:
                self.first += new - removed
        else:
            new = len(payload)
            if not self.lines:
                self.first, self.lines = first, list(payload)
            elif first < self.first:
                if first + removed < self.first:
                    raise ProtocolError(f"lines at {first} would leave a gap before {self.held}")
                drop = first + removed - self.first
                self.lines = list(payload) + self.lines[drop:]
                self.first = first
            else:
                index = first - self.first
                if index > len(self.lines):
                    raise ProtocolError(f"lines at {first} would leave a gap after {self.held}")
                self.lines[index : index + removed] = payload
        if first + removed <= self.top_line:
            # Lines changed above the viewport: stay on the same text.  The one
            # exception is a viewport at the very top, which stays at the top.
            if not (self.top_line == 1 and self.top_row == 0):
                self.top_line += new - removed
        elif first <= self.top_line:  # the top line itself was replaced
            self.top_line = max(first, min(self.top_line, first + new - 1))
            self.top_row = 0


class FrameModel:
    def __init__(self) -> None:
        self.faces: dict[int, dict[str, Any]] = {}
        self.ui_faces: dict[str, int] = {}
        self.layout: dict[str, Any] | None = None
        self.views: dict[int, ViewModel] = {}
        self.focus: int | None = None
        self.title = ""
        self.resets = 0
        # Minibuffers shown outside the text: view id -> (host, options).
        self.minibuffers: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.popup: dict[str, Any] | None = None
        self.message: tuple[str, str] | None = None  # (text, kind) of the latest message
        self.keys = ""  # a pending key sequence or prefix argument
        self.menu: list[Any] = []
        self.messages: list[tuple[str, str]] = []

    def view(self, view_id: int) -> ViewModel:
        view = self.views.get(view_id)
        if view is None:
            view = self.views[view_id] = ViewModel(view_id)
        return view

    @property
    def prompt(self) -> tuple[ViewModel, dict[str, Any]] | None:
        """The minibuffer shown in the prompt area, with its options."""
        for view_id, (host, options) in self.minibuffers.items():
            if host.get("kind") == "prompt":
                return self.view(view_id), options
        return None

    @property
    def main_view(self) -> ViewModel | None:
        if self.layout is None:
            return None
        main = self.layout["main"]
        return self.view(main[1]) if main[0] == "win" else None

    def apply(self, events: list[list[Any]]) -> list[int]:
        """Apply one batch.  Returns the sequence numbers of the flushes in it."""
        flushed: list[int] = []
        for event in events:
            name, args = event[0], event[1:]
            if name == "flush":
                flushed.append(args[0])
            elif name == "reset":
                self.faces.clear()
                self.ui_faces = {}
                self.layout = None
                self.views.clear()
                self.focus = None
                self.minibuffers.clear()
                self.popup = None
                self.keys = ""
                self.resets += 1
            elif name == "face_define":
                self.faces[args[0]] = args[1]
            elif name == "ui_faces":
                self.ui_faces = args[0]
            elif name == "layout":
                self.layout = args[0]
            elif name == "title":
                self.title = args[0]
            elif name == "focus":
                self.focus = args[0]
            elif name == "win_info":
                self.view(args[0]).info = args[1]
            elif name == "held":
                self.view(args[0]).hold(args[1], args[2])
            elif name == "lines":
                self.view(args[0]).splice(args[1], args[2], args[3])
            elif name == "selections":
                view = self.view(args[0])
                view.primary, view.selections, view.shape = args[1], args[2], args[3]
            elif name == "modeline":
                self.view(args[0]).modeline = args[1]
            elif name == "reveal":
                self.view(args[0]).pending_reveal = (args[1], args[2], args[3])
            elif name == "minibuffer_show":
                self.minibuffers[args[0]] = (args[1], args[2])
            elif name == "minibuffer_hide":
                self.minibuffers.pop(args[0], None)
                self.views.pop(args[0], None)
            elif name == "popup_show":
                self.popup = {"items": args[0], "selected": args[1], "view": args[2],
                              "line": args[3], "offset": args[4]}  # fmt: skip
            elif name == "popup_hide":
                self.popup = None
            elif name == "message":
                text = "".join(str(run[1]) for run in args[0])
                if args[1] == "keys":
                    self.keys = text
                else:
                    self.message = (text, args[1])
                    self.messages.append((text, args[1]))
            elif name == "menu":
                self.menu = args[0]
            else:
                raise ProtocolError(f"unknown redraw event: {name}")
        return flushed
