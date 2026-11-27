"""Stage 6: window splits, panels and placement (ENGINE 8.2, 8.5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.frontends.screen import arrange, tab_line
from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI


def test_arranging_splits_and_docked_panels() -> None:
    layout = {
        "main": ["split", 9, "h", [0.5, 0.5], [["win", 1], ["split", 8, "v", [0.5, 0.5], [["win", 2], ["win", 3]]]]],
        "panels": [
            {"id": 1, "name": "bottom", "visible": True, "mode": "docked", "side": "bottom", "size": 0.25,
             "tabs": [{"view": 7, "title": "a"}, {"view": 6, "title": "b"}], "active": 7},
            {"id": 2, "name": "hidden", "visible": False, "mode": "docked", "side": "left", "size": 0.3,
             "tabs": [{"view": 5, "title": "c"}], "active": 5},
        ],
    }  # fmt: skip
    placed = arrange(layout, 81, 40)
    rects = {k: (r.x, r.y, r.w, r.h) for k, r in placed.views.items()}
    assert rects == {7: (0, 31, 81, 9), 1: (0, 0, 40, 29), 2: (41, 0, 40, 14), 3: (41, 15, 40, 14)}
    assert sorted(placed.modelines) == [(1, 0, 29, 40), (2, 41, 14, 40), (3, 41, 29, 40)]
    assert placed.rules == [(40, 0, 30)]
    assert [(p["name"], x, y, w) for p, x, y, w in placed.tablines] == [("bottom", 0, 30, 81)]
    assert tab_line(layout["panels"][0], 12) == [(" a ", True), (" b ", False), ("      ", False)]
    # A screen too small for a panel draws the windows and leaves the panel out.
    assert set(arrange(layout, 81, 4).views) == {1, 2, 3}


@pytest.fixture(scope="module")
def engine() -> Iterator[Path]:
    with running_engine() as path:
        yield path


@pytest.fixture
def ui(engine: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine, rows=20, cols=60) as session:
        session.eval(
            f"_fr = ed.frames[{session.frame_id}]; _w = _fr.window\n"
            "_b = ed.buffers.new('doc', '\\n'.join(f'line {n}' for n in range(1, 41))); _w.show(_b)"
        )
        yield session
        session.eval("ed.buffers.kill(_b)")
        session.client.notify("ui.detach")


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def test_splitting_and_moving_between_windows(ui: HeadlessUI) -> None:
    first = ui.view.id
    ui.keys("C-x", "2")
    assert ui.model.layout is not None and ui.model.layout["main"][0] == "split"
    below = [w for w in ui.model.windows() if w != first][0]
    assert ui.model.windows() == [first, below] and ui.model.focus == below == ui.view.id
    grid = ui.grid()
    assert grid[0] == " 1 line 1" and grid[8].startswith(" - doc   L1 C0")
    assert grid[9] == " 1 line 1" and grid[18].startswith(" - doc   L1 C0")
    # Each window has its own cursor and its own scroll position on the one buffer.
    ui.keys("M->")
    assert value(ui, "[w.cursor.pos for w in _fr.windows()]") == value(ui, "[0, len(_b)]")
    assert ui.grid()[0] == " 1 line 1" and ui.grid()[17].strip() == "40 line 40"
    ui.type("!")  # an edit in one shows in both
    ui.keys("C-x", "o", "M->")
    assert ui.model.focus == first and ui.grid()[7].strip() == "40 line 40!"
    assert ui.cursor_position() == (7, 11)
    ui.keys("C-x", "3")  # the top window, side by side
    assert len(ui.model.windows()) == 3
    assert "│" in ui.grid()[0] and "│" not in ui.grid()[12]
    # A click in another window focuses it.
    ui.client.notify("input.mouse", 1, "press", 1, "", below, "text", 3, 2)
    ui.idle()
    assert ui.model.focus == below and value(ui, "_fr.window.cursor.pos") == value(
        ui, "_b.position(3, 2)"
    )
    ui.keys("C-x", "0")
    assert below not in ui.model.windows() and below not in ui.model.views
    ui.keys("C-x", "1")
    assert len(ui.model.windows()) == 1 and ui.model.layout["main"][0] == "win"
    ui.keys("C-x", "0")
    assert ui.message == "This is the only window"
    ui.keys("C-x", "o")
    assert ui.message == "There is nowhere else to go"
    # C-<return> is the same command.
    ui.eval("ed.message('')")
    ui.keys("C-x", "2")
    before = ui.model.focus
    ui.keys("C-<return>")
    assert ui.model.focus != before
    ui.keys("C-<return>")
    assert ui.model.focus == before
    ui.keys("C-x", "1")


def test_a_docked_panel_with_tabs(ui: HeadlessUI) -> None:
    ui.eval(
        "_one = ed.buffers.new('*one*', 'first tab'); _two = ed.buffers.new('*two*', 'second tab')\n"
        "_v1 = ed.show(_one, 'messages'); _v2 = ed.show(_two, 'search')"
    )
    panel = ui.panels()["bottom"]
    assert [t["title"] for t in panel["tabs"]] == ["*one*", "*two*"] and panel["active"] == value(
        ui, "_v2.id"
    )
    assert ui.model.focus == ui.view.id  # showing something does not take the keyboard
    grid = ui.grid()
    assert (
        grid[12].startswith(" - doc")
        and grid[13] == " *one*  *two*"
        and grid[14] == " 1 second tab"
    )
    assert ui.text_rows == 12
    ui.eval("ed.show(_one, 'messages')")  # shown again: the same tab, not another
    assert len(ui.panels()["bottom"]["tabs"]) == 2 and ui.grid()[14] == " 1 first tab"
    # Into the panel, where ordinary commands work; then round its tabs.
    ui.keys("C-x", "o")
    assert ui.model.focus == value(ui, "_v1.id") and ui.cursor_position() == (14, 3)
    ui.keys("C-e")
    ui.type("!")
    assert value(ui, "_one.text") == "first tab!"
    ui.keys("C-x", "p", "n")
    assert ui.model.focus == value(ui, "_v2.id") and ui.grid()[14] == " 1 second tab"
    ui.keys("C-x", "p", "d")  # to the next side round: the left
    panel = ui.panels()["bottom"]
    assert panel["side"] == "left"
    grid = ui.grid()
    assert (
        grid[0].startswith(" *one*  *two*")
        and "│" in grid[0]
        and grid[1].startswith(" 1 second tab")
    )
    assert grid[0].rstrip().endswith("line 1")
    ui.keys("C-x", "p", "p")  # hide: focus goes back to the text
    assert not ui.panels()["bottom"]["visible"] and ui.model.focus == ui.view.id
    assert ui.grid()[0] == " 1 line 1" and ui.text_rows == 18
    ui.keys("C-x", "p", "p")  # and back, with its tabs
    assert ui.panels()["bottom"]["visible"] and len(ui.panels()["bottom"]["tabs"]) == 2
    ui.keys("C-x", "p", "o", "C-x", "p", "k", "C-x", "p", "k")
    assert ui.panels()["bottom"]["tabs"] == [] and ui.model.focus == ui.view.id
    assert value(ui, "(ed.buffers.get('*one*') is _one, _v1.id in _fr.views)") == (True, False)
    ui.eval("_fr.panels.pop('bottom'); ed.buffers.kill(_one); ed.buffers.kill(_two)")


def test_a_floating_transient_panel_beside_the_cursor(ui: HeadlessUI) -> None:
    ui.keys("C-n", "C-n", "C-f", "C-f")
    ui.eval(
        "_peek = ed.buffers.new('*peek*', 'a definition\\nseen in passing'); _pv = ed.show(_peek, 'peek', focus=True)"
    )
    panel = ui.panels()["peek"]
    assert panel["mode"] == "floating" and panel["transient"] is True
    assert panel["anchor"] == {"view": ui.view.id, "line": 3, "offset": 2}
    grid = ui.grid()
    top = next(i for i, row in enumerate(grid) if "┌" in row)
    assert top == 3  # just under the cursor's row
    assert "│ *peek*" in grid[top + 1] and "│ 1 a definition" in grid[top + 2]
    assert grid[top].index("┌") == 5  # and starts at its column
    assert grid[0] == " 1 line 1"  # the text around the box is still drawn
    ui.keys("C-x", "o")  # focus leaves: a transient panel closes
    assert not ui.panels()["peek"]["visible"] and "┌" not in "".join(ui.grid())
    ui.eval("_fr.panels['peek'].float('center'); _fr.panels['peek'].transient = False")
    grid = ui.grid()
    top = next(i for i, row in enumerate(grid) if "┌" in row)
    assert 4 <= top <= 8 and grid[top].index("┌") == 12
    ui.eval("_fr.panels.pop('peek').close(_pv); ed.buffers.kill(_peek)")


def test_the_frontend_acts_on_a_panel_and_the_engine_answers_with_a_layout(ui: HeadlessUI) -> None:
    ui.eval(
        "_x = ed.buffers.new('*x*', 'x'); _y = ed.buffers.new('*y*', 'y'); _vx = ed.show(_x, 'help'); _vy = ed.show(_y, 'help')"
    )
    panel = ui.panels()["side"]
    assert panel["side"] == "right" and panel["active"] == value(ui, "_vy.id")
    ui.client.call("ui.panel", panel["id"], "activate_tab", value(ui, "_vx.id"))
    ui.idle()
    assert ui.panels()["side"]["active"] == value(ui, "_vx.id") == ui.model.focus
    ui.client.call("ui.panel", panel["id"], "dock", "bottom")
    ui.idle()
    assert ui.panels()["side"]["side"] == "bottom"
    ui.client.call("ui.panel", panel["id"], "float")
    ui.idle()
    assert ui.panels()["side"]["mode"] == "floating" and ui.panels()["side"]["anchor"] == "center"
    ui.client.call("ui.panel", panel["id"], "close_tab", value(ui, "_vx.id"))
    ui.idle()
    assert [t["title"] for t in ui.panels()["side"]["tabs"]] == ["*y*"]
    ui.client.call("ui.panel", panel["id"], "hide")
    ui.idle()
    assert not ui.panels()["side"]["visible"] and ui.model.focus == ui.view.id
    with pytest.raises(Exception, match="no such panel"):
        ui.client.call("ui.panel", 9999, "hide")
    with pytest.raises(Exception, match="unknown panel action"):
        ui.client.call("ui.panel", panel["id"], "explode")
    ui.eval("_fr.panels.pop('side'); ed.buffers.kill(_x); ed.buffers.kill(_y)")


def test_placement_is_one_table(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.panels import Main, InPanel\n"
        "_old = dict(ed.options.placement)\n"
        "ed.options.placement = {**_old, 'help': Main(split='h'), 'notes': InPanel('notes', dock='top', size=0.25)}\n"
        "_h = ed.buffers.new('*h*', 'help in a split'); _hv = ed.show(_h, 'help')"
    )
    assert len(ui.model.windows()) == 2 and "side" not in ui.panels()
    assert "│" in ui.grid()[0] and ui.grid()[0].rstrip().endswith("help in a split")
    assert ui.model.focus == ui.view.id
    ui.eval("ed.show(_h, 'help')")  # already on show: no further split
    assert len(ui.model.windows()) == 2
    ui.eval(
        "_n = ed.buffers.new('*n*', 'on top'); _nv = ed.show(_n, 'notes'); _u = ed.show(_b, 'no-such-role')"
    )
    assert ui.panels()["notes"]["side"] == "top" and ui.grid()[:2] == [" *n*", " 1 on top"]
    assert value(ui, "_u is _w") is True  # an unknown role is a document
    ui.eval(
        "ed.options.placement = _old; _fr.delete_window(_hv); _fr.panels.pop('notes').close(_nv)\n"
        "ed.buffers.kill(_h); ed.buffers.kill(_n)"
    )
    assert len(ui.model.windows()) == 1 and ui.grid()[0] == " 1 line 1"


def test_a_frontend_that_reattaches_gets_the_whole_arrangement(ui: HeadlessUI) -> None:
    ui.keys("C-x", "2")
    ui.eval("_m = ed.buffers.get('*messages*'); ed.show(_m, 'messages')")
    before = (ui.model.layout, ui.grid())
    resets = ui.model.resets
    ui.client.call("ui.resync")
    ui.idle()
    assert ui.model.resets == resets + 1 and (ui.model.layout, ui.grid()) == before
    ui.keys("C-x", "1")
    ui.eval("_fr.panels.pop('bottom')")


# -- paragraph attributes, resources and inline images (ENGINE 4.3) -----------------


def test_paragraph_attributes_travel_with_the_line(ui: HeadlessUI) -> None:
    ui.eval(
        "_b.replace(0, len(_b), 'Title\\nbody text\\nquoted')\n"
        "_s1 = _b.layer('para').add(0, 5, line={'align': 'center', 'spacing_after': 0.5})\n"
        "_s2 = _b.layer('para').add(_b.line_start(3), len(_b), line={'indent': 4})"
    )
    assert (ui.view.line(1) or [{}])[0] == {"align": "center", "spacing_after": 0.5}
    assert (ui.view.line(2) or [{}])[0] == {}
    assert (ui.view.line(3) or [{}])[0] == {"indent": 4}
    rows = ui.grid()[:3]
    assert rows[0] == " 1" + " " * 27 + "Title"  # centred in the 57 columns beside the gutter
    assert rows[1] == " 2 body text" and rows[2] == " 3     quoted"
    # The cursor and geometry account for the padding.
    ui.keys("M-<", "C-f")
    assert ui.cursor_position() == (0, 3 + 26 + 1)
    ui.eval("_w.selections.set([(_b.position(2, 6),) * 2])")
    ui.keys("<down>")  # straight down from column 6: two characters into the indented line
    assert ui.cursors() == [(3, 2)]
    ui.eval("_s1.remove(); _s2.remove()")
    assert ui.grid()[0] == " 1 Title"


def test_an_inline_image_is_a_resource_defined_once(ui: HeadlessUI, tmp_path: Path) -> None:
    picture = tmp_path / "dot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    ui.eval(
        f"_img = ed.resources.image({str(picture)!r}); _again = ed.resources.image({str(picture)!r})\n"
        "_b.replace(0, len(_b), 'see X here\\nand X again')\n"
        "_i1 = _b.layer('img').add(4, 5, display={'image': _img, 'width': 16, 'height': 16, 'alt': 'dot'})\n"
        "_i2 = _b.layer('img').add(_b.line_start(2) + 4, _b.line_start(2) + 5, display={'image': _img, 'alt': 'dot'})"
    )
    assert value(ui, "_img == _again") is True
    resource = value(ui, "_img")
    assert ui.model.resources == {resource: ("image", {"path": str(picture.resolve())})}
    assert (ui.view.line(1) or [{}, []])[1] == [
        [0, "see ", 0],
        [4, {"image": resource, "width": 16, "height": 16, "alt": "dot", "covers": 1}],
        [5, " here", 0],
    ]
    assert ui.grid()[:2] == [" 1 see [dot] here", " 2 and [dot] again"]
    # The picture stands for one character: the cursor steps over it as one.
    ui.keys("M-<", "C-f", "C-f", "C-f", "C-f")
    assert ui.cursor_position() == (0, 7)
    ui.keys("C-f")
    assert ui.cursor_position() == (0, 12)
    # A frontend that starts again is told about the resource again.
    ui.client.call("ui.resync")
    ui.idle()
    assert ui.model.resources == {resource: ("image", {"path": str(picture.resolve())})}
    # From bytes, for a resource that is not a file.
    ui.eval(
        "_raw = ed.resources.image(data=b'GIF89a', mime='image/gif'); _i1.set(display={'image': _raw, 'alt': 'gif'})"
    )
    assert ui.model.resources[value(ui, "_raw")] == (
        "image",
        {"mime": "image/gif", "data": "R0lGODlh"},
    )
    assert ui.grid()[0] == " 1 see [gif] here"


def test_a_frontend_that_does_not_draw_panels_gets_one_window(engine: Path) -> None:
    with HeadlessUI(engine, rows=12, cols=50, attach={"capabilities": []}) as plain:
        plain.eval(
            f"_pf = ed.frames[{plain.frame_id}]; _pb = ed.buffers.new('plain', 'text'); _pf.window.show(_pb)"
        )
        plain.call("describe_command", "kill_line")
        assert plain.panels() == {} and plain.view.info["name"] == "*help*"
        assert plain.text_rows_shown()[0] == " 1 kill_line"
        plain.keys("q")
        assert plain.view.info["name"] == "plain"
        plain.keys("C-x", "2")
        assert (
            plain.message == "This frontend shows one window only"
            and len(plain.model.windows()) == 1
        )
        plain.eval("ed.buffers.kill(_pb)")
        plain.client.notify("ui.detach")


def test_a_window_with_one_row_shows_its_text_and_no_modeline() -> None:
    layout = {
        "main": ["split", 9, "v", [0.34, 0.33, 0.33], [["win", 1], ["win", 2], ["win", 3]]],
        "panels": [],
    }
    for rows in range(1, 9):
        placed = arrange(layout, 40, rows)
        covered: dict[int, str] = {}
        for view, rect in placed.views.items():
            assert rect.h >= 1
            for y in range(rect.y, rect.y + rect.h):
                assert y not in covered, (rows, "text over text")
                covered[y] = f"text {view}"
        for view, _x, y, _w in placed.modelines:
            assert y not in covered, (rows, f"modeline of {view} over {covered.get(y)}")
            covered[y] = f"modeline {view}"
        assert all(0 <= y < rows for y in covered)
    assert set(arrange(layout, 40, 3).views) == {1, 2, 3} and arrange(layout, 40, 3).modelines == []
    assert len(arrange(layout, 40, 6).modelines) == 3


def test_the_frontend_resizes_a_split_and_a_panel(ui: HeadlessUI) -> None:
    ui.keys("C-x", "2")
    split = ui.model.layout["main"]
    assert split[3] == [0.5, 0.5]
    ui.client.call("view.split_resized", split[1], [3, 1])
    ui.idle()
    assert ui.model.layout["main"][3] == [0.75, 0.25]
    rows = [ui.arrangement.views[w].h for w in ui.model.windows()]
    assert rows[0] > 2 * rows[1]
    for bad in ([1], [1, 0], ["a", 1]):
        with pytest.raises(Exception, match="ratios|positive"):
            ui.client.call("view.split_resized", split[1], bad)
    with pytest.raises(Exception, match="no such split"):
        ui.client.call("view.split_resized", 99999, [1, 1])
    ui.keys("C-x", "1")
    ui.eval("_m = ed.buffers.get('*messages*'); ed.show(_m, 'messages')")
    panel = ui.panels()["bottom"]
    ui.client.call("ui.panel", panel["id"], "resize", 0.5)
    ui.idle()
    assert ui.panels()["bottom"]["size"] == 0.5
    with pytest.raises(Exception, match="not a size"):
        ui.client.call("ui.panel", panel["id"], "resize", "big")
    ui.eval("_fr.panels.pop('bottom')")
