"""Editor tabs (WORKSPACE.md, stage W-3): each window's tabs, as the engine
keeps them and as a frontend that draws the workbench is told."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import running_engine
from neomacs.testing.client import RemoteError
from neomacs.testing.headless import HeadlessUI

WORKBENCH = {"capabilities": ["panels", "workbench"]}


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def ui() -> Iterator[HeadlessUI]:
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70, attach=WORKBENCH) as session:
        session.eval(f"_f = ed.frames[{session.frame_id}]")
        yield session


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    made: list[Path] = []
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        file = tmp_path / name
        file.write_text(f"{name}\n")
        made.append(file)
    return made


def tabs(ui: HeadlessUI, window: int = 0) -> list[str]:
    """The window's tabs as the engine has them, the one showing in brackets."""
    return value(
        ui,
        f"[(f'[{{b.name}}]' if b is w.buffer else b.name) "
        f"for w in [_f.windows()[{window}]] for b in w.tabs]",
    )


def sent(ui: HeadlessUI) -> list[dict[str, Any]]:
    """The tabs of the focused window as the frontend was told."""
    return ui.view.info["tabs"]


def test_each_buffer_shown_in_a_window_is_a_tab_of_it(ui: HeadlessUI, files: list[Path]) -> None:
    # The untouched scratch buffer is a stand-in: the first file takes its place.
    assert tabs(ui) == ["[*scratch*]"]
    for file in files[:3]:
        ui.open(file)
    assert tabs(ui) == ["a.txt", "b.txt", "[c.txt]"]
    ui.open(files[0])  # shown again: the same tab, in the same place
    assert tabs(ui) == ["[a.txt]", "b.txt", "c.txt"]
    assert sent(ui) == [
        {"title": "a.txt", "modified": False, "active": True, "path": str(files[0].resolve())},
        {"title": "b.txt", "modified": False, "active": False, "path": str(files[1].resolve())},
        {"title": "c.txt", "modified": False, "active": False, "path": str(files[2].resolve())},
    ]
    ui.type("x")
    assert [tab["modified"] for tab in sent(ui)] == [True, False, False]
    ui.keys("C-x", "C-s")
    assert [tab["modified"] for tab in sent(ui)] == [False, False, False]


def test_a_scratch_buffer_that_was_typed_in_keeps_its_tab(
    ui: HeadlessUI, files: list[Path]
) -> None:
    ui.type("notes")
    ui.open(files[0])
    assert tabs(ui) == ["*scratch*", "[a.txt]"]
    assert "path" not in sent(ui)[0]


def test_stepping_through_the_tabs_and_moving_one(ui: HeadlessUI, files: list[Path]) -> None:
    for file in files[:3]:
        ui.open(file)
    ui.keys("s-}")
    assert tabs(ui) == ["[a.txt]", "b.txt", "c.txt"]  # after the last, the first
    ui.keys("s-{")
    assert tabs(ui) == ["a.txt", "b.txt", "[c.txt]"]
    ui.keys("s-{")
    assert tabs(ui) == ["a.txt", "[b.txt]", "c.txt"]
    ui.call("tabs.move_left")
    assert tabs(ui) == ["[b.txt]", "a.txt", "c.txt"]
    assert [tab["title"] for tab in sent(ui)] == ["b.txt", "a.txt", "c.txt"]
    ui.call("tabs.move_left")
    assert ui.message == "The tab is at the end already"
    ui.call("tabs.move_right")
    ui.call("tabs.move_right")
    assert tabs(ui) == ["a.txt", "c.txt", "[b.txt]"]
    ui.call("tabs.close_others")
    assert tabs(ui) == ["[b.txt]"]
    ui.call("tabs.next")
    assert ui.message == "The window has no other tab"


def test_a_tab_keeps_the_cursor_it_had(ui: HeadlessUI, files: list[Path]) -> None:
    ui.open(files[0])
    ui.keys("C-e")
    ui.open(files[1])
    ui.keys("s-{")
    assert tabs(ui) == ["[a.txt]", "b.txt"] and value(ui, "_f.window.cursor.pos") == 5


def test_closing_a_tab_shows_the_one_to_its_left_and_keeps_the_buffer(
    ui: HeadlessUI, files: list[Path]
) -> None:
    for file in files[:3]:
        ui.open(file)
    ui.keys("s-w")
    assert tabs(ui) == ["a.txt", "[b.txt]"]
    ui.keys("s-{", "s-w")  # the first tab: the one that takes its place
    assert tabs(ui) == ["[b.txt]"]
    assert value(ui, "sorted(b.name for b in ed.buffers if b.file)") == ["a.txt", "b.txt", "c.txt"]
    # The last tab of the only window: the window stays, with the scratch buffer.
    ui.keys("s-w")
    assert tabs(ui) == ["[*scratch*]"] and len(value(ui, "[w.id for w in _f.windows()]")) == 1
    ui.keys("s-w")
    assert ui.message == "This is the only tab" and tabs(ui) == ["[*scratch*]"]


def test_a_new_window_has_one_tab_and_closing_its_last_removes_it(
    ui: HeadlessUI, files: list[Path]
) -> None:
    ui.open(files[0])
    ui.open(files[1])
    ui.keys("C-x", "3")
    assert tabs(ui, 0) == ["a.txt", "[b.txt]"] and tabs(ui, 1) == ["[b.txt]"]
    ui.open(files[2])  # in the new window only
    assert tabs(ui, 0) == ["a.txt", "[b.txt]"] and tabs(ui, 1) == ["b.txt", "[c.txt]"]
    ui.keys("s-w", "s-w")
    assert value(ui, "len(_f.windows())") == 1 and tabs(ui) == ["a.txt", "[b.txt]"]
    assert value(ui, "_f.focus is _f.windows()[0]") is True


def test_killing_a_buffer_takes_its_tab_from_every_window(
    ui: HeadlessUI, files: list[Path]
) -> None:
    for file in files[:3]:
        ui.open(file)
    ui.keys("C-x", "3")
    ui.keys("s-{")  # the second window shows... it has one tab; open another
    ui.open(files[1])
    assert tabs(ui, 0) == ["a.txt", "b.txt", "[c.txt]"] and tabs(ui, 1) == ["c.txt", "[b.txt]"]
    ui.keys("C-x", "k")  # b.txt: showing in the second window, a tab at rest in the first
    assert tabs(ui, 0) == ["a.txt", "[c.txt]"] and tabs(ui, 1) == ["[c.txt]"]
    # Killed by a program, not the command: the tabs go all the same.
    ui.eval("ed.buffers.kill(ed.buffers.get('a.txt'))")
    assert tabs(ui, 0) == ["[c.txt]"]
    ui.eval("ed.buffers.kill(ed.buffers.get('c.txt'))")
    assert tabs(ui, 0) == ["[*scratch*]"] and tabs(ui, 1) == ["[*scratch*]"]


def test_a_click_on_a_tab(ui: HeadlessUI, files: list[Path]) -> None:
    for file in files[:3]:
        ui.open(file)
    ui.keys("C-x", "3")
    first, second = value(ui, "[w.id for w in _f.windows()]")
    assert value(ui, "_f.focus.id") == second
    ui.client.notify("ui.tab", first, "activate", 0)
    ui.idle()
    assert tabs(ui, 0) == ["[a.txt]", "b.txt", "c.txt"]
    assert value(ui, "_f.focus.id") == first  # the keyboard goes to the window clicked in
    ui.client.notify("ui.tab", first, "move", 0, 2)
    ui.idle()
    assert tabs(ui, 0) == ["b.txt", "c.txt", "[a.txt]"]
    ui.client.notify("ui.tab", first, "close", 0)  # one that is not showing
    ui.idle()
    assert tabs(ui, 0) == ["c.txt", "[a.txt]"]
    ui.client.notify("ui.tab", first, "close", 1)
    ui.client.notify("ui.tab", first, "close", 0)
    ui.idle()
    assert value(ui, "[w.id for w in _f.windows()]") == [second]  # its last tab: the window goes
    ui.client.notify("ui.tab", second, "close", 0)
    ui.idle()
    assert tabs(ui) == ["[*scratch*]"]
    ui.client.notify("ui.tab", second, "close", 0)
    ui.idle()
    assert ui.message == "This is the only tab"
    for params in (
        (second, "close", 5),
        (second, "move", 0, 9),
        (second, "burn", 0),
        (999, "close", 0),
    ):
        with pytest.raises(RemoteError):
            ui.client.call("ui.tab", *params)
    assert tabs(ui) == ["[*scratch*]"]


def test_tabs_are_told_only_to_a_frontend_that_draws_them(files: list[Path]) -> None:
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70) as plain:
        plain.open(files[0])
        plain.open(files[1])
        assert "tabs" not in plain.view.info
        plain.eval(f"_f = ed.frames[{plain.frame_id}]")
        plain.keys("s-{")  # the commands work all the same
        assert tabs(plain) == ["[a.txt]", "b.txt"]
        with HeadlessUI(path, rows=14, cols=70, attach=WORKBENCH) as second:
            assert [tab["title"] for tab in second.view.info["tabs"]] == ["*scratch*"]
            second.call("view.toggle_tabs")
            assert "tabs" not in second.view.info and second.message == "Tabs are hidden"
            second.call("view.toggle_tabs")
            assert "tabs" in second.view.info


def test_each_project_keeps_its_windows_tabs(ui: HeadlessUI, tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        for file in ("one.txt", "two.txt"):
            (tmp_path / name / f"{name[0]}-{file}").write_text("text\n")
        ui.call("project.add", str(tmp_path / name))
        ui.open(tmp_path / name / f"{name[0]}-one.txt")
        ui.open(tmp_path / name / f"{name[0]}-two.txt")
    assert tabs(ui) == ["b-one.txt", "[b-two.txt]"]
    ui.call("project.switch", "alpha")
    assert tabs(ui) == ["a-one.txt", "[a-two.txt]"]
    assert [tab["title"] for tab in sent(ui)] == ["a-one.txt", "a-two.txt"]
    # A buffer killed while its project is put away: the tab is gone when it comes back.
    ui.eval("ed.buffers.kill(ed.buffers.get('b-one.txt'))")
    ui.eval("ed.buffers.kill(ed.buffers.get('b-two.txt'))")
    ui.call("project.switch", "beta")
    assert tabs(ui) == ["[*scratch*]"]


def test_a_buffer_saved_into_another_project_leaves_the_tabs_of_the_one_it_left(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        ui.call("project.add", str(tmp_path / name))
    (tmp_path / "alpha" / "keep.txt").write_text("keep\n")
    (tmp_path / "alpha" / "move.txt").write_text("move\n")
    ui.open(tmp_path / "alpha" / "keep.txt")
    ui.open(tmp_path / "alpha" / "move.txt")
    ui.keys("C-x", "3")
    ui.keys("s-{")
    ui.open(tmp_path / "alpha" / "keep.txt")
    assert tabs(ui, 0) == ["keep.txt", "[move.txt]"] and tabs(ui, 1) == ["move.txt", "[keep.txt]"]
    # Saved from a program, with the buffer showing in one window and at rest in the other.
    ui.eval(f"_b = ed.buffers.get('move.txt'); _b.save({str(tmp_path / 'beta' / 'moved.txt')!r})")
    assert value(ui, "ed.workspaces.of(_b).name") == "beta"
    ui.call("project.switch", "alpha")
    assert tabs(ui, 0) == ["[keep.txt]"] and tabs(ui, 1) == ["[keep.txt]"]
