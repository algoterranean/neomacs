"""The Python frontends: monospace layout, session replay, terminal input and drawing."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from neomacs.frontends.model import FrameModel, ViewModel
from neomacs.frontends.monolayout import MonoView, cluster_width
from neomacs.frontends.tui import parse_keys, sgr

SESSION = Path(__file__).parent / "data" / "sessions" / "basic.json"


def view_of(lines: list[str], **info: object) -> ViewModel:
    v = ViewModel(1)
    v.info = {"line_count": len(lines), "wrap": True, "tab_width": 4, "gutter": [], **info}
    # As the engine fills a viewport: existing lines, not yet held, replaced by their content.
    v.splice(1, len(lines), [[{}, [[0, text, 0]] if text else []] for text in lines])
    return v


# -- monolayout ---------------------------------------------------------------


def test_cluster_widths() -> None:
    assert [cluster_width(c) for c in ("a", "中", "é", "é", "👍🏽", "👨‍👩‍👧")] == [
        1, 2, 1, 1, 2, 2,
    ]  # fmt: skip


def test_rows_wrap_and_tabs() -> None:
    mono = MonoView(view_of(["abcdefghij", ""]), width=4, height=5)
    rows = mono.rows(1)
    assert [("".join(c.text for c in r.cells), r.start, r.end, r.last) for r in rows] == [
        ("abcd", 0, 4, False), ("efgh", 4, 8, False), ("ij", 8, 10, True),
    ]  # fmt: skip
    assert mono.render() == ["abcd", "efgh", "ij", "", ""]
    assert [len(mono.rows(n)) for n in (1, 2, 99)] == [3, 1, 1]  # 99 is not held
    tabs = MonoView(view_of(["\tx", "ab\tc", "abcd\te"]), width=20, height=3)
    assert tabs.render() == ["    x", "ab  c", "abcd    e"]  # tab stops every 4 columns
    assert tabs.locate(2, 3) == (0, 4)
    wide = MonoView(view_of(["a中文b"]), width=3, height=3)
    assert wide.render() == ["a中", "文b", ""]  # a wide character is not split


def test_no_wrap() -> None:
    mono = MonoView(view_of(["abcdefghij"], wrap=False), width=4, height=2)
    assert len(mono.rows(1)) == 1


def test_locate_and_offset_at() -> None:
    mono = MonoView(view_of(["abcdefghij", "中文x"]), width=4, height=10)
    assert [mono.locate(1, o) for o in (0, 3, 4, 9, 10)] == [(0, 0), (0, 3), (1, 0), (2, 1), (2, 2)]
    assert mono.locate(2, 1) == (0, 2) and mono.locate(2, 2) == (1, 0)  # 'x' wraps
    rows = mono.rows(1)
    assert mono.offset_at(rows[0], 2) == 2
    assert mono.offset_at(rows[0], 99) == 3  # stays on a wrapped row
    assert mono.offset_at(rows[2], 99) == 10  # the end of the line on its last row
    assert mono.offset_at(mono.rows(2)[0], 1) == 0  # the second column of a wide character


def test_scrolling_reveal_and_viewport() -> None:
    mono = MonoView(view_of([f"line {n}" for n in range(1, 51)]), width=20, height=5)
    assert mono.viewport() == (1, 5, 5)
    mono.reveal(3, 0, "nearest")  # already visible: no movement
    assert mono.viewport() == (1, 5, 5)
    mono.reveal(20, 0, "nearest")  # below: it becomes the last row
    assert mono.viewport() == (16, 20, 5)
    mono.reveal(10, 0, "nearest")  # above: it becomes the first
    assert mono.viewport() == (10, 14, 5)
    mono.reveal(30, 0, "center")
    assert mono.viewport() == (28, 32, 5)
    mono.reveal(50, 0, "top")
    assert mono.viewport() == (50, 50, 5)
    assert mono.render()[1:] == ["", "", "", ""]
    mono.scroll_rows(-1000)
    assert mono.viewport()[0] == 1
    mono.scroll_rows(7)
    assert mono.cursor_cell(8, 2) == (0, 2) and mono.cursor_cell(1, 0) is None
    mono.resize(20, 10)
    assert mono.viewport() == (8, 17, 10)


def test_layout_requests() -> None:
    mono = MonoView(view_of(["abcdefghij", "xy", "0123456789"]), width=4, height=4)
    assert mono.move([[1, 1, None]], "visual_line", 1) == [[1, 5, 1]]
    assert mono.move([[1, 5, 1], [1, 9, None]], "visual_line", 2) == [[2, 1, 1], [3, 1, 1]]
    assert mono.move([[2, 2, 3]], "visual_line", 1) == [[3, 3, 3]]  # the goal outlives a short line
    assert mono.move([[1, 0, None]], "visual_line", -5) == [[1, 0, 0]]  # stops at the top
    assert mono.move([[3, 9, None]], "visual_line", 5) == [[3, 9, 1]]  # and at the bottom
    assert mono.line_edge(1, 5, "start") == [1, 4] and mono.line_edge(1, 5, "end") == [1, 7]
    assert mono.line_edge(1, 9, "end") == [1, 10]
    assert mono.rect(1, 5) == {"x": 1, "y": 1, "width": 1, "height": 1}
    # A page is the height less one: three rows, which is all of line 1.
    assert mono.move([[1, 0, None]], "page", 1) == [[2, 0, 0]]
    assert (mono.model.top_line, mono.model.top_row) == (2, 0)  # and the view scrolled with it


def test_gutter() -> None:
    v = view_of(["one", "two"], gutter=[{"id": "signs", "kind": "marks", "width": 1},
                                         {"id": "numbers", "kind": "numbers"}])  # fmt: skip
    v.lines[1][0]["gutter"] = {"signs": ["●", 3]}
    mono = MonoView(v, width=12, height=2)
    assert mono.gutter_width == 5 and mono.text_width == 7
    assert mono.render() == ["   1 one", "●  2 two"]
    assert mono.cursor_cell(2, 1) == (1, 6)


# -- conformance replay ---------------------------------------------------------


def test_recorded_session_replays_to_the_recorded_state() -> None:
    recording = json.loads(SESSION.read_text(encoding="utf-8"))
    assert recording["protocol"] == 1
    model = FrameModel()
    seqs: list[int] = []
    for batch in recording["batches"]:
        seqs.extend(model.apply(batch))
    assert seqs == sorted(seqs) and len(seqs) == len(recording["batches"])
    final = recording["final"]
    assert {str(k): v for k, v in sorted(model.faces.items())} == final["faces"]
    assert model.ui_faces == final["ui_faces"]
    assert model.layout == final["layout"] and model.focus == final["focus"]
    for view_id, expected in final["views"].items():
        view = model.views[int(view_id)]
        assert view.info == expected["info"]
        assert (view.first, view.lines) == (expected["first"], expected["lines"])
        assert view.selections == expected["selections"]
        assert view.modeline == expected["modeline"]


# -- the terminal frontend ------------------------------------------------------


def test_parse_keys() -> None:
    assert parse_keys(b"a B\r\t\x7f") == (
        ["a", "<space>", "B", "<return>", "<tab>", "<backspace>"],
        b"",
    )
    assert parse_keys(b"\x01\x05\x1f\x00") == (["C-a", "C-e", "C-_", "C-<space>"], b"")
    assert parse_keys(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F") == (
        ["<up>", "<down>", "<right>", "<left>", "<home>", "<end>"], b"",
    )  # fmt: skip
    assert parse_keys(b"\x1b[5~\x1b[6~\x1b[3~\x1bOA") == (
        ["<prior>", "<next>", "<delete>", "<up>"],
        b"",
    )
    assert parse_keys(b"\x1b[1;5C\x1b[1;2A\x1b[1;3D") == (["C-<right>", "S-<up>", "M-<left>"], b"")
    assert parse_keys(b"\x1bf\x1b<\x1b\x7f") == (["M-f", "M-<", "M-<backspace>"], b"")
    assert parse_keys("é中👍".encode()) == (["é", "中", "👍"], b"")
    assert parse_keys(b"\x1b") == (["<escape>"], b"")
    # Input split in the middle of a character or an escape sequence is kept for the next read.
    keys, rest = parse_keys("中".encode()[:2])
    assert keys == [] and parse_keys(rest + "中".encode()[2:]) == (["中"], b"")
    keys, rest = parse_keys(b"x\x1b[1;")
    assert keys == ["x"] and parse_keys(rest + b"5C") == (["C-<right>"], b"")


def test_sgr() -> None:
    base = {"fg": "#d4d4d4", "bg": "#1e1e1e"}
    assert sgr({}, base) == "\x1b[0;38;2;212;212;212;48;2;30;30;30m"
    assert sgr({"fg": "#ff0000", "weight": "bold", "slant": "italic", "underline": True}, base) == (
        "\x1b[0;38;2;255;0;0;48;2;30;30;30;1;3;4m"
    )


def read_until(fd: int, needle: str, timeout: float = 10.0) -> str:
    seen = ""
    deadline = time.monotonic() + timeout
    while needle not in seen:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{needle!r} never appeared in:\n{seen[-2000:]}")
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            try:
                seen += os.read(fd, 65536).decode("utf-8", errors="replace")
            except OSError:
                break
    return seen


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_tui_in_a_pseudo_terminal(tmp_path: Path) -> None:
    path = tmp_path / "poem.txt"
    path.write_text("first line\nsecond line\n")
    fd, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    proc = subprocess.Popen(
        [sys.executable, "-m", "neomacs", "--tui", str(path)],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )  # fmt: skip
    os.close(slave)
    try:
        out = read_until(fd, "poem.txt")  # the modeline is drawn last
        assert "\x1b[?1049h" in out and "first line" in out and "second line" in out
        os.write(fd, b"\x1b[B\x1b[F")  # down, end
        os.write(fd, b" edited in a terminal")
        read_until(fd, "second line edited in a terminal")
        os.write(fd, b"\x1f")  # C-_ undoes the last character
        read_until(fd, "L2 C31")
        os.write(fd, b"\x11")  # C-q quits
        tail = read_until(fd, "\x1b[?1049l")
        assert "\x1b[?25h" in tail
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(fd)
    assert path.read_text() == "first line\nsecond line\n"  # nothing saves yet
