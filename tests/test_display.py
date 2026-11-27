"""Stage 2: the view builder, frames, pacing, reattachment, geometry (ENGINE 4, 8)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from neomacs.display.faces import FaceTable
from neomacs.display.view import build_line
from neomacs.frontends.model import FrameModel, ProtocolError, ViewModel
from neomacs.testing import Client, RemoteError, running_engine
from neomacs.testing.headless import HeadlessUI
from neomacs.text import Buffer


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=10, cols=40) as session:
        session.eval(f"_b = ed.buffers.new('test'); ed.frames[{session.frame_id}].window.show(_b)")
        yield session
        session.client.notify("ui.detach")
        session.client.eval("ed.buffers.kill(_b)")


# -- the view builder -------------------------------------------------------


def test_plain_line() -> None:
    b = Buffer("t", "hello\n\nworld")
    faces = FaceTable()
    assert build_line(b, 1, faces, []) == [{}, [[0, "hello", 0]]]
    assert build_line(b, 2, faces, []) == [{}, []]
    assert build_line(b, 3, faces, []) == [{}, [[0, "world", 0]]]


def test_spans_become_runs_by_priority() -> None:
    b = Buffer("t", "one two three")
    faces = FaceTable()
    layer = b.layer("x")
    layer.add(4, 13, priority=1, face="string")
    layer.add(8, 13, priority=5, face="bold")
    attrs, runs = build_line(b, 1, faces, [])
    assert attrs == {}
    assert [(r[0], r[1]) for r in runs] == [(0, "one "), (4, "two "), (8, "three")]
    assert faces.attrs(runs[1][2]) == {"fg": "#ce9178"}
    assert faces.attrs(runs[2][2]) == {"fg": "#ce9178", "weight": "bold"}
    assert runs[0][2] == 0


def test_providers_invisible_virtual_text_and_gutter() -> None:
    b = Buffer("t", "abcdef\nnext")
    faces = FaceTable()

    def provider(buf: Buffer, start: int, end: int) -> list[tuple[int, int, str, int]]:
        return [(0, 2, "keyword", 0), (100, 200, "comment", 0)]

    layer = b.layer("x")
    layer.add(2, 4, invisible=True)
    layer.add(4, 6, after_text=" <-", face="comment")
    layer.add(0, 0, gutter={"column": "signs", "text": "●", "face": "error"})
    layer.add(0, 6, line={"align": "center"})
    attrs, runs = build_line(b, 1, faces, [provider])
    assert attrs["align"] == "center"
    assert attrs["gutter"] == {"signs": ["●", faces.id_for("error")]}
    assert [(r[0], r[1]) for r in runs] == [(0, "ab"), (4, "ef"), (None, " <-")]
    assert runs[0][2] == faces.id_for(("keyword",))
    # The second line is untouched by any of it.
    assert build_line(b, 2, faces, [provider]) == [{}, [[0, "next", 0]]]


def test_face_table() -> None:
    faces = FaceTable()
    assert faces.id_for(()) == 0 and faces.id_for("default") == 0
    a = faces.id_for(("keyword", "bold"))
    assert faces.id_for(("keyword", "bold")) == a != faces.id_for(("bold", "keyword"))
    generation = faces.generation
    faces.define("keyword", fg="#ffffff")
    assert faces.generation == generation + 1
    assert faces.attrs(a) == {"fg": "#ffffff", "weight": "bold"}
    with pytest.raises(ValueError):
        faces.define("x", colour="red")


# -- the frontend model -----------------------------------------------------


def line(text: str) -> list[object]:
    return [{}, [[0, text, 0]]]


def texts(view: ViewModel) -> list[str]:
    return ["".join(r[1] for r in ln[1]) for ln in view.lines]


def test_model_splices() -> None:
    v = ViewModel(1)
    v.splice(10, 3, [line("a"), line("b"), line("c")])  # the first fill defines what is held
    assert v.held == (10, 12)
    v.splice(11, 1, [line("B1"), line("B2")])  # replace inside
    assert (v.held, texts(v)) == ((10, 13), ["a", "B1", "B2", "c"])
    v.splice(14, 2, [line("d"), line("e")])  # fill just below
    assert (v.held, texts(v)[-2:]) == ((10, 15), ["d", "e"])
    v.splice(8, 2, [line("y"), line("z")])  # fill just above
    assert (v.held, texts(v)[:3]) == ((8, 15), ["y", "z", "a"])
    v.splice(2, 1, 4)  # three lines inserted far above: renumber only
    assert v.held == (11, 18)
    v.splice(30, 5, 0)  # lines removed below: nothing held changes
    assert v.held == (11, 18)
    v.splice(9, 4, [line("X")])  # a replacement that starts above and reaches in
    assert (v.held, texts(v)[:2]) == ((9, 15), ["X", "a"])
    v.hold(10, 12)
    assert (v.held, texts(v)) == ((10, 12), ["a", "B1", "B2"])
    v.hold(1, 0)
    assert v.held is None
    v.splice(20, 2, [line("p"), line("q")])
    with pytest.raises(ProtocolError):
        v.splice(25, 0, [line("gap below")])
    with pytest.raises(ProtocolError):
        v.splice(5, 0, [line("gap above")])


def test_model_keeps_scroll_position_on_the_same_text() -> None:
    v = ViewModel(1)
    v.splice(1, 0, [line(str(n)) for n in range(1, 21)])
    v.top_line = 10
    v.splice(3, 0, [line("new")])  # inserted above
    assert v.top_line == 11
    v.splice(1, 2, 0)  # two removed above
    assert v.top_line == 9
    v.splice(15, 1, [line("below")])
    assert v.top_line == 9
    v.splice(9, 1, [line("replaced")])
    assert v.top_line == 9 and v.top_row == 0


def test_model_rejects_unknown_events() -> None:
    with pytest.raises(ProtocolError):
        FrameModel().apply([["mystery", 1]])


# -- end to end -------------------------------------------------------------


def test_attach_sends_full_state(ui: HeadlessUI) -> None:
    model = ui.model
    assert model.resets == 1
    assert model.layout == {"main": ["win", ui.view.id], "panels": []}
    assert model.focus == ui.view.id
    assert 0 in model.faces and "fg" in model.faces[0]
    assert set(model.ui_faces) >= {"selection", "cursor", "gutter", "modeline"}
    assert all(face_id in model.faces for face_id in model.ui_faces.values())
    info = ui.view.info
    assert info["line_count"] == 1 and info["tab_width"] == 8 and info["wrap"] is True
    assert info["gutter"][0]["kind"] == "numbers"
    assert ui.view.selections == [[1, 0, 1, 0]]


def test_typing_is_displayed(ui: HeadlessUI) -> None:
    ui.type("hello world\nsecond line")
    assert ui.text_rows_shown() == [" 1 hello world", " 2 second line"]
    assert ui.cursor() == (1, 14)
    assert ui.screen()[-1] == " * test   L2 C11 "
    ui.keys("<up>", "<home>")
    ui.type(">> ")
    assert ui.text_rows_shown()[0] == " 1 >> hello world"
    assert ui.cursor() == (0, 6)
    ui.keys("C-/", "C-/", "C-/")
    assert ui.text_rows_shown()[0] == " 1 hello world"
    ui.keys("<end>", "<backspace>", "<backspace>")
    assert ui.text_rows_shown()[0] == " 1 hello wor"


def test_only_changed_lines_are_resent(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 6)))")
    before = [id(x) for x in ui.view.lines]
    ui.eval("_b.insert(_b.line_start(3), '>')")
    after = [id(x) for x in ui.view.lines]
    assert [a == b for a, b in zip(before, after, strict=True)] == [True, True, False, True, True]
    assert ui.text_rows_shown()[2] == " 3 >line 3"


def test_wrapping_wide_characters_and_tabs(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'x' * 45 + '\\n\\tab\\n中文 wide\\ne\\u0301 é')")
    rows = ui.text_rows_shown()
    assert rows[0] == " 1 " + "x" * 37 and rows[1] == "   " + "x" * 8  # 40 columns, 3 of gutter
    assert rows[2] == " 2         ab"
    assert rows[3] == " 3 中文 wide"
    ui.eval(f"ed.frames[{ui.frame_id}].window.cursor.move_to(_b.line_start(3) + 2)")
    assert ui.cursor() == (3, 3 + 4)  # two wide characters take four columns
    ui.eval(f"ed.frames[{ui.frame_id}].window.cursor.move_to(_b.line_start(4) + 2)")
    assert ui.cursor() == (4, 3 + 1)  # a letter and its combining accent take one


def test_visual_line_motion_asks_the_frontend(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'a' * 90 + '\\nshort\\n' + 'b' * 50)")
    ui.keys("M-<", "<right>", "<right>", "<right>", "<right>", "<right>")
    assert ui.cursor() == (0, 3 + 5)
    ui.keys("<down>")  # the next visual row of the same wrapped line
    assert ui.cursor() == (1, 3 + 5) and ui.view.selections[0][2:] == [1, 37 + 5]
    ui.keys("<down>", "<down>")  # the third row, then on to line 2
    assert ui.view.selections[0][2:] == [2, 5]
    ui.keys("<down>")  # line 3 is long enough; the goal column is kept
    assert ui.view.selections[0][2:] == [3, 5]
    ui.keys("<end>", "<up>")  # line 3 wraps: from its second row to its first, same column
    assert ui.view.selections[0][2:] == [3, 13]
    ui.keys("<up>")  # 'short' is shorter than column 13: its end
    assert ui.view.selections[0][2:] == [2, 5]
    ui.keys("<up>")  # the goal column survives: column 13 of the last row of line 1
    assert ui.view.selections[0][2:] == [1, 74 + 13]


def test_geometry_falls_back_when_the_frontend_cannot_answer(engine_path: Path) -> None:
    with HeadlessUI(engine_path, rows=6, cols=20) as ui:
        ui.eval(f"_g = ed.buffers.new('g', 'one\\ntwo\\nthree'); "
                f"ed.frames[{ui.frame_id}].window.show(_g)")  # fmt: skip
        del ui.client.request_handlers["layout.move"]
        ui.keys("<down>", "<down>")
        assert ui.view.selections[0][2:] == [3, 0]
        ui.client.notify("ui.detach")


def test_scrolling_fetches_lines_and_drops_distant_ones(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 2001)))")
    held = ui.view.held
    assert held is not None and held[0] == 1 and held[1] < 200  # a margin, not the whole file
    ui.keys("M->")
    assert ui.view.selections[0][2] == 2000
    assert "2000 line 2000" in ui.text_rows_shown()[-1]
    held = ui.view.held
    assert held is not None and held[0] > 1800 and held[1] == 2000
    ui.scroll(-500)  # the wheel: the frontend scrolls and reports
    ui.idle()
    rows = ui.text_rows_shown()
    assert rows[0].startswith("1492 ") and all("line" in r for r in rows)
    ui.keys("<next>", "<next>")
    assert ui.cursor() is not None


def test_edits_outside_what_is_held_only_renumber(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 2001)))")
    ui.keys("M->")
    first_before = ui.view.first
    kept = [id(x) for x in ui.view.lines]
    ui.eval("_b.insert(0, 'top\\nlines\\n')")
    assert ui.view.first == first_before + 2
    assert [id(x) for x in ui.view.lines] == kept  # nothing was re-sent
    assert ui.view.line_count == 2002
    assert "2002 line 2000" in ui.text_rows_shown()[-1]  # same text, renumbered, still in view


def test_whole_line_edits_are_reported_exactly() -> None:
    b = Buffer("t", "one\ntwo\nthree\n")
    seen: list[tuple[int, int, int]] = []
    b.add_listener(lambda change: seen.append(change.lines))
    b.insert(b.line_start(2), "new\n")  # a whole line, at the start of a line
    b.delete(b.line_start(1), b.line_start(2))  # a whole line removed
    b.replace(b.line_start(2), b.line_start(3), "TWO\nand a half\n")  # one line became two
    b.insert(b.line_start(2), "x")  # inside a line
    b.insert(b.line_start(2) + 1, "\n")  # splitting a line
    b.insert(b.line_start(1), "no newline")  # at a line start, but not a whole line
    b.delete(b.line_end(1), b.line_start(3))  # joins two lines and takes a third
    assert seen == [(2, 0, 1), (1, 1, 0), (2, 1, 2), (2, 1, 1), (2, 1, 2), (1, 1, 1), (1, 3, 1)]


def test_changed_ranges_are_tracked_separately() -> None:
    from neomacs.window import MAX_DIRTY_RANGES, merge_dirty

    ranges = merge_dirty([], 10, 1, 1)
    ranges = merge_dirty(ranges, 50, 1, 3)  # far below: its own range
    assert ranges == [(10, 1, 1), (50, 1, 3)]
    ranges = merge_dirty(ranges, 2, 0, 2)  # inserted above: the others move down
    assert ranges == [(2, 0, 2), (12, 1, 1), (52, 1, 3)]
    ranges = merge_dirty(ranges, 13, 1, 1)  # touching one: merged with it
    assert ranges == [(2, 0, 2), (12, 2, 2), (52, 1, 3)]
    ranges = merge_dirty(ranges, 10, 45, 1)  # spanning two: merged, with the lines between
    assert ranges == [(2, 0, 2), (10, 43, 1)]
    assert merge_dirty([(5, 0, 2)], 5, 2, 0) == []  # inserted, then deleted again
    many: list[tuple[int, int, int]] = []
    for n in range(MAX_DIRTY_RANGES + 5):
        many = merge_dirty(many, 1 + n * 10, 1, 2)
    # Too many to track: they were folded into one range, and tracking went on from there.
    assert len(many) <= MAX_DIRTY_RANGES and many[0][0] == 1 and many[0][1] > 100
    assert sum(new - old for _, old, new in many) == MAX_DIRTY_RANGES + 5


def test_edits_on_both_sides_do_not_resend_the_middle(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 31)))")
    before = {n: id(ui.view.line(n)) for n in range(1, 31)}
    ui.eval("_b.insert(_b.line_start(3), 'A'); _b.insert(_b.line_start(25), 'B'); "
            "_b.insert(_b.line_start(10), 'whole\\n')")  # fmt: skip
    assert ui.view.line_count == 31
    changed = sorted(
        n for n in range(1, 31) if id(ui.view.line(n if n < 10 else n + 1)) != before[n]
    )
    assert changed == [3, 25]  # only the two edited lines; the inserted one is new
    text = lambda n: "".join(r[1] for r in ui.view.line(n)[1])  # noqa: E731
    assert [text(n) for n in (3, 10, 11, 26)] == ["Aline 3", "whole", "line 10", "Bline 25"]


def test_a_view_at_the_top_stays_at_the_top(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 101)))")
    ui.eval("_b.insert(0, 'new first\\n')")
    assert ui.text_rows_shown()[0] == "  1 new first"  # at the top: the new line is shown
    ui.scroll(20)
    ui.idle()
    top = ui.text_rows_shown()[0]
    assert top == " 21 line 20"
    ui.eval("_b.insert(0, 'another\\n'); _b.insert(_b.line_start(5), 'and one more\\n')")
    assert ui.text_rows_shown()[0] == " 23 line 20"  # scrolled: the same text stays in view
    ui.eval("_b.delete(0, _b.line_start(11))")
    assert ui.text_rows_shown()[0] == "13 line 20"  # (the gutter narrowed: under 100 lines now)


def test_a_huge_insert_is_not_sent_unasked(ui: HeadlessUI) -> None:
    ui.type("first\nlast")
    ui.eval("_b.insert(_b.line_start(2), '\\n'.join(f'mid {n}' for n in range(5000)) + '\\n')")
    held = ui.view.held
    assert held is not None and held[1] - held[0] < 300
    assert ui.view.line_count == 5002
    assert ui.text_rows_shown()[1].endswith("mid 0")


def test_spans_and_faces_reach_the_frontend(ui: HeadlessUI) -> None:
    ui.type("plain styled")
    ui.eval("_s = _b.layer('t').add(6, 12, face='keyword')")
    runs = ui.view.lines[0][1]
    assert [(r[0], r[1]) for r in runs] == [(0, "plain "), (6, "styled")]
    assert ui.model.faces[runs[1][2]] == {"fg": "#c586c0"}
    ui.eval("ed.faces.define('keyword', fg='#112233', weight='bold')")  # a theme change
    assert ui.model.faces[runs[1][2]] == {"fg": "#112233", "weight": "bold"}
    ui.eval("_s.remove()")
    assert [(r[0], r[1]) for r in ui.view.lines[0][1]] == [(0, "plain styled")]
    ui.eval("ed.faces.define('keyword', fg='#c586c0')")


def test_gutter_marks_are_displayed(ui: HeadlessUI) -> None:
    ui.type("one\ntwo")
    ui.eval(
        "_b.locals['gutter_columns'] = ["
        "{'id': 'signs', 'kind': 'marks', 'side': 'left', 'width': 1},"
        "{'id': 'numbers', 'kind': 'numbers', 'side': 'left', 'mode': 'absolute'}]\n"
        "_b.layer('d').add(_b.line_start(2), _b.line_start(2), "
        "gutter={'column': 'signs', 'text': '●', 'face': 'error'})"
    )
    assert ui.text_rows_shown() == ["   1 one", "●  2 two"]


def test_showing_another_buffer_replaces_what_is_held(ui: HeadlessUI) -> None:
    ui.type("in test")
    ui.eval(f"_o = ed.buffers.new('other', 'other text'); "
            f"ed.frames[{ui.frame_id}].window.show(_o)")  # fmt: skip
    assert ui.text_rows_shown() == [" 1 other text"]
    assert ui.view.info["name"] == "other"
    ui.eval(f"ed.frames[{ui.frame_id}].window.show(_b)")
    assert ui.text_rows_shown() == [" 1 in test"]


# -- pacing (ENGINE 4.8) ----------------------------------------------------


def wait_for(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.005)


def test_one_batch_in_flight_and_then_the_current_state(engine_path: Path) -> None:
    with HeadlessUI(engine_path, rows=8, cols=30, auto_ack=False) as ui:
        wait_for(lambda: ui.batches == 1)
        ui.ack()
        wait_for(lambda: ui.batches == 2)  # the lines for the viewport it reported
        ui.ack()
        ui.client.eval(f"_p = ed.buffers.new('p'); ed.frames[{ui.frame_id}].window.show(_p)")
        wait_for(lambda: ui.batches == 3)
        seen = ui.batches
        for n in range(50):  # fifty edits while a batch is unacknowledged
            ui.client.eval(f"_p.insert(len(_p), 'edit {n}\\n')")
        for ch in "typed":  # and keys, which must all arrive in order
            ui.client.notify("input.key", ch)
        ui.client.eval("0")
        time.sleep(0.05)
        assert ui.batches == seen  # nothing more was sent
        ui.ack()
        wait_for(lambda: len(ui.unacked) == 1)
        while ui.unacked:  # at most a couple more: viewport follow-ups
            ui.ack()
            time.sleep(0.02)
        assert ui.batches <= seen + 3
        # The cursor sat where the text was appended, so it moved along with it.
        assert ui.client.eval("_p.text.endswith('edit 48\\nedit 49\\ntyped')") == "True"
        ui.auto_ack = True
        ui.ack()
        ui.idle()
        assert ui.text_rows_shown()[-1].endswith("51 typed")
        ui.client.notify("ui.detach")


def test_geometry_request_forces_a_send(engine_path: Path) -> None:
    with HeadlessUI(engine_path, rows=8, cols=30, auto_ack=False) as ui:
        ui.auto_ack = True
        ui.ack()
        ui.idle()
        ui.eval(f"_q = ed.buffers.new('q', 'aaa\\nbbb\\nccc'); "
                f"ed.frames[{ui.frame_id}].window.show(_q)")  # fmt: skip
        ui.auto_ack = False
        ui.client.eval("_q.insert(0, 'new first line\\n')")  # one batch goes out, unacknowledged
        wait_for(lambda: len(ui.unacked) == 1)
        ui.client.eval("_q.insert(0, 'another\\n')")  # this one is held back...
        ui.client.notify("input.key", "<down>")  # ...until a geometry request needs it
        wait_for(lambda: len(ui.unacked) >= 2)
        ui.auto_ack = True
        ui.ack()
        ui.idle()
        # The cursor was at the start, so both insertions pushed it down; then one more line.
        assert ui.view.selections[0][2:] == [4, 0]
        assert ui.text_rows_shown()[:2] == [" 1 another", " 2 new first line"]
        ui.client.notify("ui.detach")


def test_a_slow_frontend_does_not_delay_a_fast_one(engine_path: Path) -> None:
    with (
        HeadlessUI(engine_path, rows=8, cols=30, auto_ack=False) as slow,
        HeadlessUI(engine_path, rows=8, cols=30) as fast,
    ):
        fast.eval(f"_f = ed.buffers.new('f'); ed.frames[{fast.frame_id}].window.show(_f); "
                  f"ed.frames[{slow.frame_id}].window.show(_f)")  # fmt: skip
        fast.type("visible at once")
        assert fast.text_rows_shown() == [" 1 visible at once"]
        assert slow.batches <= 1
        slow.auto_ack = True
        slow.ack()
        slow.idle()
        assert slow.text_rows_shown() == [" 1 visible at once"]
        for session in (slow, fast):
            session.client.notify("ui.detach")


def test_stalled_frontend_is_noticed(engine_path: Path) -> None:
    with HeadlessUI(engine_path, rows=8, cols=30, auto_ack=False) as ui:
        ui.client.eval("import neomacs.window as w; _old = w.STALL_SECONDS; w.STALL_SECONDS = 0.05")
        try:
            ui.ack()
            ui.client.eval("ed.buffers.get('*scratch*').insert(0, 'x')")
            wait_for(lambda: ui.client.eval(f"ed.frames[{ui.frame_id}].stalled") == "True")
            assert "stopped acknowledging" in ui.client.eval("ed.messages[-1]")
            assert ui.client.call("engine.idle", 1.0) is True  # a stalled frame is not waited for
            ui.ack()
            wait_for(lambda: ui.client.eval(f"ed.frames[{ui.frame_id}].stalled") == "False")
        finally:
            ui.client.eval("w.STALL_SECONDS = _old; ed.buffers.get('*scratch*').delete(0, 1)")
            ui.client.notify("ui.detach")


# -- frames, reattachment, protocol version (ENGINE 4.6, 8.4; STABILITY 5) --


def test_two_frontends_on_one_buffer(engine_path: Path) -> None:
    with (
        HeadlessUI(engine_path, rows=8, cols=30) as a,
        HeadlessUI(engine_path, rows=5, cols=20) as b,
    ):
        a.eval(f"_s = ed.buffers.new('shared'); ed.frames[{a.frame_id}].window.show(_s); "
               f"ed.frames[{b.frame_id}].window.show(_s)")  # fmt: skip
        a.type("from a")
        b.idle()
        assert b.text_rows_shown() == [" 1 from a"]
        b.keys("<home>")
        b.type("b: ")
        a.idle()
        assert a.text_rows_shown() == [" 1 b: from a"]
        # Each keeps its own cursor: a's was at the end and moved with the text.
        assert a.view.selections[0][2:] == [1, 9] and b.view.selections[0][2:] == [1, 3]
        b.client.notify("ui.detach")
        b.close()
        a.type("!")
        assert a.text_rows_shown() == [" 1 b: from a!"]
        a.client.notify("ui.detach")


def test_resync_reproduces_the_state(ui: HeadlessUI) -> None:
    ui.type("some text\nmore")
    ui.eval("_b.layer('t').add(0, 4, face='keyword')")
    before = (ui.screen(), ui.view.selections, ui.view.lines, dict(ui.model.faces))
    resets = ui.model.resets
    ui.client.call("ui.resync")
    ui.idle()
    assert ui.model.resets == resets + 1
    assert (ui.screen(), ui.view.selections, ui.view.lines, dict(ui.model.faces)) == before


def test_reattach_after_losing_the_connection(engine_path: Path) -> None:
    first = HeadlessUI(engine_path, rows=8, cols=70)
    first.eval(f"_r = ed.buffers.new('r'); ed.frames[{first.frame_id}].window.show(_r)")
    first.type("kept across the drop")
    first.keys("<home>", "<right>", "<right>")
    frame, token, screen = first.frame_id, first.token, first.screen()
    first.close()  # no ui.detach: the connection just goes
    with Client(engine_path) as probe:
        wait_for(lambda: probe.eval(f"ed.frames[{frame}].attached") == "False")
        probe.eval("_r.insert(len(_r), ' (edited while away)')")
        with pytest.raises(RemoteError) as info:
            probe.call("ui.attach", {"protocol": 1, "frame": frame, "token": "wrong"})
        assert info.value.code == -32002
    with HeadlessUI(engine_path, rows=8, cols=70, attach={"frame": frame, "token": token}) as back:
        assert back.frame_id == frame
        assert back.screen()[0] == screen[0] + " (edited while away)"
        assert back.view.selections[0][2:] == [1, 2]  # the cursor is where it was left
        with Client(engine_path) as probe, pytest.raises(RemoteError) as info:
            probe.call("ui.attach", {"protocol": 1, "frame": frame, "token": token})
        assert info.value.code == -32003  # in use
        back.client.notify("ui.detach")


def test_request_to_a_lost_frontend_fails_with_frontend_gone(engine_path: Path) -> None:
    ui = HeadlessUI(engine_path, rows=8, cols=30)
    frame = ui.frame_id
    ui.client.request_handlers["layout.viewport"] = lambda view: time.sleep(1.0)
    ui.client.eval(
        "import asyncio\n_gone = []\n"
        "async def _ask(frame):\n"
        "    try:\n"
        "        await frame.request('layout.viewport', frame.window.id, timeout=5)\n"
        "    except Exception as exc:\n"
        "        _gone.append(type(exc).__name__)\n"
        f"ed.keep_task(asyncio.get_running_loop().create_task(_ask(ed.frames[{frame}])))"
    )
    time.sleep(0.05)
    ui.close()
    with Client(engine_path) as probe:
        wait_for(lambda: probe.eval("_gone") == "['FrontendGone']")
        probe.eval(f"ed.frames[{frame}].delete()")


def test_detached_frame_expires(engine_path: Path) -> None:
    with Client(engine_path) as probe:
        probe.eval("_t = ed.frame_detach_timeout; ed.frame_detach_timeout = 0.05")
        try:
            ui = HeadlessUI(engine_path, rows=8, cols=30)
            frame, token = ui.frame_id, ui.token
            ui.close()
            wait_for(lambda: probe.eval(f"{frame} in ed.frames") == "False")
            with pytest.raises(RemoteError) as info:
                probe.call("ui.attach", {"protocol": 1, "frame": frame, "token": token})
            assert info.value.code == -32002
        finally:
            probe.eval("ed.frame_detach_timeout = _t")


def test_protocol_version_must_match(client: Client) -> None:
    with pytest.raises(RemoteError) as info:
        client.call("ui.attach", {"protocol": 999})
    assert info.value.code == -32001
    assert info.value.data == {"engine": 1, "frontend": 999}
    assert "999" in str(info.value)
    with pytest.raises(RemoteError):
        client.call("ui.attach", {})
    reply = client.call("ui.attach", {"protocol": 1, "capabilities": ["made-up"]})
    assert reply["protocol"] == 1 and reply["capabilities"] == []
    with pytest.raises(RemoteError, match="already has a frame"):
        client.call("ui.attach", {"protocol": 1})
    client.notify("ui.detach")


def test_input_before_attaching_is_refused(client: Client) -> None:
    with pytest.raises(RemoteError, match="not attached"):
        client.call("ui.resync")


def test_detach_deletes_the_frame() -> None:
    with running_engine() as path, HeadlessUI(path) as ui:
        frame = ui.frame_id
        ui.client.notify("ui.detach")
        assert ui.client.eval(f"{frame} in ed.frames") == "False"
