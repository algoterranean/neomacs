"""The reference terminal frontend.

It draws what `monolayout` lays out, translates terminal input into the
engine's key notation, and acknowledges each batch after painting it.
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import signal
import sys
import termios
import tty
from pathlib import Path
from typing import Any

from neomacs.frontends.session import Session
from neomacs.testing.client import Client

QUIT_KEY = "C-q"

_CSI_KEYS = {
    "A": "<up>", "B": "<down>", "C": "<right>", "D": "<left>", "H": "<home>", "F": "<end>",
    "1~": "<home>", "2~": "<insert>", "3~": "<delete>", "4~": "<end>", "5~": "<prior>",
    "6~": "<next>", "7~": "<home>", "8~": "<end>", "Z": "S-<tab>",
}  # fmt: skip
_SS3_KEYS = {"A": "<up>", "B": "<down>", "C": "<right>", "D": "<left>", "H": "<home>",
             "F": "<end>", "P": "<f1>", "Q": "<f2>", "R": "<f3>", "S": "<f4>"}  # fmt: skip


def _plain(code: int) -> str:
    """Key notation for one code point with no escape prefix."""
    if code == 0x0D or code == 0x0A:
        return "<return>"
    if code == 0x09:
        return "<tab>"
    if code == 0x7F or code == 0x08:
        return "<backspace>"
    if code == 0x1B:
        return "<escape>"
    if code == 0x20:
        return "<space>"
    if code == 0:
        return "C-<space>"
    if code < 0x20:
        return "C-" + chr(code + 0x60) if code <= 26 else "C-" + chr(code + 0x40).lower()
    return chr(code)


def parse_keys(data: bytes) -> tuple[list[str], bytes]:
    """Translate terminal input into keys.  Returns the keys and any incomplete tail."""
    keys: list[str] = []
    text = data.decode("utf-8", errors="surrogateescape")
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\x1b":
            if "\udc80" <= ch <= "\udcff":  # an incomplete UTF-8 sequence at the end
                return keys, text[i:].encode("utf-8", errors="surrogateescape")
            keys.append(_plain(ord(ch)))
            i += 1
            continue
        if i + 1 >= len(text):
            keys.append("<escape>")
            i += 1
        elif text[i + 1] == "[":
            j = i + 2
            while j < len(text) and not ("@" <= text[j] <= "~"):
                j += 1
            if j >= len(text):
                return keys, text[i:].encode("utf-8", errors="surrogateescape")
            body = text[i + 2 : j + 1]
            key = _CSI_KEYS.get(body)
            if key is None and ";" in body:  # modified keys: 1;5C is Control-right
                params, final = body[:-1].split(";"), body[-1]
                base = _CSI_KEYS.get(final) or _CSI_KEYS.get(params[0] + final)
                if base is not None and params[-1].isdigit():
                    bits = int(params[-1]) - 1
                    prefix = ("C-" if bits & 4 else "") + ("M-" if bits & 2 else "")
                    key = prefix + ("S-" if bits & 1 else "") + base
            if key is not None:
                keys.append(key)
            i = j + 1
        elif text[i + 1] == "O" and i + 2 < len(text):
            key = _SS3_KEYS.get(text[i + 2])
            if key is not None:
                keys.append(key)
            i += 3
        else:
            keys.append("M-" + _plain(ord(text[i + 1])))
            i += 2
    return keys, b""


def _rgb(colour: str) -> tuple[int, int, int] | None:
    if len(colour) == 7 and colour[0] == "#":
        try:
            return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)
        except ValueError:
            return None
    return None


def sgr(attrs: dict[str, Any], base: dict[str, Any]) -> str:
    """The escape sequence that selects a face, over the default face BASE."""
    merged = {**base, **attrs}
    codes = ["0"]
    fg = _rgb(str(merged.get("fg", "")))
    bg = _rgb(str(merged.get("bg", "")))
    if fg is not None:
        codes.append("38;2;{};{};{}".format(*fg))
    if bg is not None:
        codes.append("48;2;{};{};{}".format(*bg))
    if merged.get("weight") == "bold":
        codes.append("1")
    if merged.get("slant") == "italic":
        codes.append("3")
    if merged.get("underline"):
        codes.append("4")
    if merged.get("strike"):
        codes.append("9")
    return "\x1b[" + ";".join(codes) + "m"


class Terminal:
    """Raw mode and the alternate screen, restored on exit."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved: list[Any] | None = None

    def __enter__(self) -> Terminal:
        self._saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.write("\x1b[?1049h\x1b[?25l")
        return self

    def __exit__(self, *exc: object) -> None:
        self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def write(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        while data:
            written = os.write(sys.stdout.fileno(), data)
            data = data[written:]

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.lines, size.columns


class Tui:
    def __init__(self, session: Session, terminal: Terminal) -> None:
        self.session = session
        self.terminal = terminal
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        session.on_redraw = self.wake

    def wake(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")

    # -- drawing ------------------------------------------------------------

    def draw(self) -> None:
        session = self.session
        with session.lock:
            if session.model.main_view is None:
                return
            faces = session.model.faces
            base = faces.get(0, {})
            width = session.cols
            main_rows = session.main_rows
            out: list[str] = ["\x1b[?25l\x1b[H"]
            plain = sgr({}, base)
            own = {-1: plain + "\x1b[7m", -2: plain + "\x1b[2m"}  # the active tab; other chrome
            for y, cells in enumerate(session.compose()):
                out.append(f"\x1b[{y + 1};1H")
                current: int | None = None
                for text, face in cells:
                    if face != current:
                        current = face
                        style = own[face] if face < 0 else sgr(faces.get(face, {}), base)
                        out.append("\x1b[0m" + style)
                    out.append(text)
            out.append("\x1b[0m")

            # Under the main area: the prompt, or the latest message.
            prompt = session.model.prompt
            label = session.prompt_label()
            bottom: list[str] = []
            if prompt is not None:
                rendered = session.layout(prompt[0]).render()
                bottom = [
                    (label if i == 0 else " " * len(label)) + r for i, r in enumerate(rendered)
                ]
            else:
                bottom = [session.bottom_text()]
            for i in range(session.prompt_rows):
                text = bottom[i] if i < len(bottom) else ""
                out.append(f"\x1b[{main_rows + 1 + i};1H" + plain + text[:width].ljust(width))

            # Completion candidates, drawn over the last rows of the main area.
            popup = session.model.popup
            if popup is not None and popup["items"]:
                items = popup["items"][: max(1, min(8, main_rows - 2))]
                first_row = main_rows - len(items) + 1
                for i, item in enumerate(items):
                    line = f" {item[0]}  {item[1]}"[:width].ljust(width)
                    style = "\x1b[7m" if i == popup["selected"] else ""
                    out.append(f"\x1b[{first_row + i};1H" + plain + style + line + "\x1b[0m")

            cursor = session.cursor_position()
            if cursor is not None:
                out.append(f"\x1b[{cursor[0] + 1};{cursor[1] + 1}H\x1b[?25h")
        self.terminal.write("".join(out))
        session.ack()

    # -- the loop -----------------------------------------------------------

    def run(self) -> None:
        session = self.session
        client = session.client
        resized = [False]

        def on_resize(signum: int, frame: object) -> None:
            resized[0] = True
            self.wake()

        previous = signal.signal(signal.SIGWINCH, on_resize)
        pending = b""
        try:
            self.draw()
            while not client.closed.is_set():
                try:
                    ready, _, _ = select.select([self.terminal.fd, self._wake_r], [], [], 0.5)
                except InterruptedError:
                    ready = []
                if resized[0]:
                    resized[0] = False
                    session.resize(*self.terminal.size())
                    self.draw()
                if self._wake_r in ready:
                    os.read(self._wake_r, 4096)
                    self.draw()
                if self.terminal.fd in ready:
                    data = os.read(self.terminal.fd, 4096)
                    if not data:
                        return
                    keys, pending = parse_keys(pending + data)
                    for key in keys:
                        if key == QUIT_KEY:
                            return
                        client.notify("input.key", key)
                        if key == "C-g":
                            client.notify("input.interrupt")
        finally:
            signal.signal(signal.SIGWINCH, previous)


def run(socket_path: Path | str, file: str | None = None) -> None:
    """Attach to the engine at SOCKET_PATH and run until the user quits."""
    if not sys.stdin.isatty():
        raise SystemExit("the terminal frontend needs a terminal")
    client = Client(socket_path)
    with Terminal() as terminal:
        rows, cols = terminal.size()
        session = Session(client, rows, cols, auto_ack=False)
        if file is not None:
            client.call("engine.call", "files.find", [str(Path(file).expanduser().resolve())])
        Tui(session, terminal).run()
    with contextlib.suppress(OSError):
        client.notify("ui.detach")
    client.close()
