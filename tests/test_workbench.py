"""The workbench, as the engine describes it to a frontend (WORKSPACE.md, stage W-2)."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI

WORKBENCH = {"capabilities": ["panels", "workbench"]}


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def ui() -> Iterator[HeadlessUI]:
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70, attach=WORKBENCH) as session:
        session.eval(f"_f = ed.frames[{session.frame_id}]")
        yield session


def texts(items: list[dict[str, Any]]) -> list[str]:
    return [item["text"] for item in items]


def shown(ui: HeadlessUI) -> dict[str, bool]:
    return {name: region["shown"] for name, region in ui.model.regions.items()}


def test_a_frontend_that_draws_the_workbench_is_told_what_is_in_it(ui: HeadlessUI) -> None:
    assert shown(ui) == {
        "meta": True, "project": True, "details": True, "options": False, "status": True,
        "bottom": False,
    }  # fmt: skip
    assert ui.model.regions["project"]["width"] == 11.0 and "width" not in ui.model.regions["meta"]
    assert ui.model.workspaces == [
        {"id": ui.model.workspaces[0]["id"], "name": "No project", "root": None, "current": True,
         "unsaved": 0, "folders": []}
    ]  # fmt: skip
    left, right = ui.model.status
    assert texts(left) == ["No project"] and left[0]["command"] == "project.switch"
    assert texts(right) == ["Ln 1, Col 1", "Python", "UTF-8", "LF"]
    assert right[0]["command"] == "goto_line"


def test_one_that_does_not_is_told_none_of_it() -> None:
    with running_engine() as path, HeadlessUI(path, rows=10, cols=60) as plain:
        assert plain.model.regions == {} and plain.model.workspaces == []
        assert plain.model.status == ([], [])
        plain.keys("M-x")
        plain.type("view.toggle_details_sidebar")
        plain.keys("<return>")
        assert plain.message == "This frontend draws no sidebars or bars"


def test_regions_are_shown_hidden_and_resized_by_commands_and_by_the_frontend(
    ui: HeadlessUI,
) -> None:
    ui.keys("s-b")
    assert shown(ui)["details"] is False and ui.message == "The details sidebar is hidden"
    ui.call("view.toggle_options_sidebar")
    assert shown(ui)["options"] is True
    # The frontend reports what the user dragged or clicked.
    ui.client.notify("ui.region", "project", None, 14.256)
    ui.client.notify("ui.region", "details", True, None)
    ui.client.notify("ui.region", "options", None, 500)
    ui.client.notify("ui.region", "no-such-region", True, 3)
    ui.idle()
    regions = ui.model.regions
    assert (regions["project"]["width"], regions["details"]["shown"]) == (14.26, True)
    assert regions["options"]["width"] == 60.0  # kept within reason
    ui.call("view.toggle_workbench")
    assert not any(v for name, v in shown(ui).items()) and ui.message == "Only the text is shown"
    ui.call("view.toggle_workbench")
    assert shown(ui) == {
        "meta": True, "project": True, "details": True, "options": False, "status": True,
        "bottom": False,
    }  # fmt: skip
    assert ui.model.regions["project"]["width"] == 14.26  # a width is kept while hidden


def test_the_bottom_panel_has_a_toggle_too(ui: HeadlessUI) -> None:
    ui.keys("M-x")
    ui.type("view.toggle_bottom_panel")
    ui.keys("<return>")
    assert ui.message == "There is nothing in the bottom panel"
    ui.eval("ed.show(ed.buffers.get('*messages*'), 'messages')")
    assert shown(ui)["bottom"] is True
    ui.call("view.toggle_bottom_panel")
    assert shown(ui)["bottom"] is False
    ui.call("view.toggle_bottom_panel")
    assert shown(ui)["bottom"] is True


def test_the_project_list_follows_what_happens(ui: HeadlessUI, tmp_path: Path) -> None:
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    for root in (alpha, beta):
        root.mkdir()
        (root / "file.txt").write_text("x\n")
    ui.call("project.add", str(alpha))
    ui.call("project.add", str(beta))
    listed = [(w["name"], w["current"], w["unsaved"]) for w in ui.model.workspaces]
    assert listed == [("alpha", False, 0), ("beta", True, 0), ("No project", False, 0)]
    assert ui.model.workspaces[0]["root"] == str(alpha.resolve())
    assert ui.model.title == "beta - Neomacs"
    # A click in the sidebar is the command, given the project's id.
    ui.call("project.switch", ui.model.workspaces[0]["id"])
    assert [w["current"] for w in ui.model.workspaces] == [True, False, False]
    assert texts(ui.model.status[0])[0] == "alpha"
    ui.open(alpha / "file.txt")
    ui.type("unsaved")
    assert ui.model.workspaces[0]["unsaved"] == 1
    ui.call("project.switch", "beta")
    assert [(w["name"], w["unsaved"]) for w in ui.model.workspaces][0] == ("alpha", 1)
    ui.call("project.switch", "alpha")
    ui.keys("C-x", "C-s")
    assert ui.model.workspaces[0]["unsaved"] == 0
    # Renamed, moved and removed from the sidebar's menu, by id.
    ident = ui.model.workspaces[1]["id"]
    ui.call("project.rename", ident, "second")
    ui.call("project.move", ident, 0)
    assert [w["name"] for w in ui.model.workspaces] == ["second", "alpha", "No project"]
    ui.call("project.remove", ident)
    assert [w["name"] for w in ui.model.workspaces] == ["alpha", "No project"]
    ui.call("project.next")
    assert ui.model.title == "Neomacs"


def test_the_status_bar_describes_the_frame_and_the_focused_window(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        subprocess.run(
            ["git", "init", "-q", "-b", "trunk", str(repo)], check=True, capture_output=True
        )
    except OSError, subprocess.CalledProcessError:
        pytest.skip("no git that can make a repository here")
    (repo / "notes.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ui.call("project.add", str(repo))
    ui.open(repo / "notes.txt")
    left, right = ui.model.status
    assert texts(left) == ["repo", "⎇ trunk"]
    assert texts(right) == ["Ln 1, Col 1", "Text", "UTF-8", "CRLF"]
    ui.keys("<down>", "C-f", "C-f")
    assert texts(ui.model.status[1])[0] == "Ln 2, Col 3"
    ui.keys("C-M-n")  # a second cursor
    assert texts(ui.model.status[1])[:2] == ["Ln 3, Col 3", "2 selections"]
    ui.keys("C-g")
    # The right-hand part stays the window's while a prompt has the keyboard.
    ui.keys("M-x")
    assert texts(ui.model.status[1])[0].startswith("Ln ")
    ui.keys("C-g")
    # A task that is running is named, with what it is doing.
    ui.eval(
        "async def _long():\n    await asyncio.sleep(30)\n"
        "_t = ed.start_task(_long(), name='indexing'); _t.progress = 'half way'"
    )
    ui.eval("ed.update_frames()")
    assert "⟳ indexing: half way" in texts(ui.model.status[0])
    ui.eval("_t.cancel()")
    deadline = time.monotonic() + 5
    while "⟳ indexing: half way" in texts(ui.model.status[0]):
        assert time.monotonic() < deadline
        ui.idle()
        time.sleep(0.02)


def test_a_new_window_starts_plain_when_the_option_says_so(ui: HeadlessUI) -> None:
    ui.eval("ed.options.workbench = False")
    path = value(ui, "str(ed.listen_path)")
    with HeadlessUI(path, rows=10, cols=60, attach=WORKBENCH) as second:
        assert not any(region["shown"] for region in second.model.regions.values())
        second.call("view.toggle_status_bar")
        assert second.model.regions["status"]["shown"] is True
        second.client.notify("ui.detach")
    assert shown(ui)["meta"] is True  # the window that was open is as it was


def test_everything_is_sent_again_after_a_resync(ui: HeadlessUI, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    ui.call("project.add", str(tmp_path / "alpha"))
    before = (ui.model.regions, ui.model.workspaces, ui.model.status)
    ui.model.regions, ui.model.workspaces, ui.model.status = {}, [], ([], [])
    ui.client.call("ui.resync")
    ui.idle()
    assert (ui.model.regions, ui.model.workspaces, ui.model.status) == before


def test_the_bar_describes_the_view_with_the_keyboard_and_a_click_acts_there(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    (tmp_path / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    ui.open(tmp_path / "long.txt")
    ui.eval("ed.show(ed.buffers.get('*messages*'), 'messages', focus=True)")
    ui.eval("ed.frame.focus.selections.set([(0, 0)])")
    # A panel has the keyboard: the bar is about the panel's buffer.
    assert value(ui, "ed.frame.focus.buffer.name") == "*messages*"
    assert texts(ui.model.status[1])[0] == "Ln 1, Col 1"
    ui.eval("_f.focus_view(_f.window); _f.window.selections.set([(12, 12)])")
    assert texts(ui.model.status[1])[0] == "Ln 2, Col 6"
    # A prompt has it: the bar stays the window's, and a click on the
    # position asks about that window, not the prompt.
    ui.keys("M-x")
    assert texts(ui.model.status[1])[0] == "Ln 2, Col 6"
    ui.client.notify("ui.status", "right", 0)
    ui.idle()
    assert ui.prompt == "Go to line: "
    ui.type("30")
    ui.keys("<return>")
    assert value(ui, "(_f.window.buffer.name, _f.window.buffer.line_of(_f.window.cursor.pos))") == (
        "long.txt", 30,
    )  # fmt: skip
    # The question that was open before the click was given up, not left behind.
    assert ui.prompt is None and value(ui, "_f.focus is _f.window") is True
    # Which item, not which command: one that is not there, or has none, does nothing.
    for side, index in (("right", 99), ("right", 2), ("left", -1), ("nowhere", 0)):
        ui.client.notify("ui.status", side, index)
    ui.idle()
    assert ui.prompt is None


def test_a_click_on_the_bar_gives_up_a_search_in_a_place_of_its_own(tmp_path: Path) -> None:
    """A box in a named place is not among the frame's prompts; it is given
    up all the same, or it would stay open with the keyboard elsewhere."""
    (tmp_path / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    attach = {**WORKBENCH, "places": ["search"]}
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70, attach=attach) as ui:
        ui.eval(f"_f = ed.frames[{ui.frame_id}]")
        ui.open(tmp_path / "long.txt")
        ui.keys("C-s")
        ui.type("line")
        assert ui.prompt is None and len(ui.model.minibuffers) == 1
        assert value(ui, "[m.host[0] for m in _f.minibuffers.values()]") == ["place"]
        ui.client.notify("ui.status", "right", 0)
        ui.idle()
        # The search has gone; the only question open is the one the click asked.
        assert ui.prompt == "Go to line: "
        assert value(ui, "[m.host[0] for m in _f.minibuffers.values()]") == ["prompt"]
        ui.type("30")
        ui.keys("<return>")
        assert value(ui, "_f.window.buffer.line_of(_f.window.cursor.pos)") == 30
        assert ui.model.minibuffers == {} and value(ui, "_f.focus is _f.window") is True
        ui.type("x")  # the keyboard is the window's again
        assert value(ui, "_f.window.buffer.text.splitlines()[29]") == "xline 30"
