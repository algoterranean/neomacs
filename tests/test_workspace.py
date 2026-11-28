"""Projects in the engine (WORKSPACE.md, stage W-1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def ui() -> Iterator[HeadlessUI]:
    # An engine of its own: projects are the editor's, and one test's would be another's.
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70) as session:
        session.eval(f"_f = ed.frames[{session.frame_id}]")
        yield session


def tree(root: Path, *names: str) -> Path:
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"{name}\n")
    return root


def add(ui: HeadlessUI, root: Path) -> None:
    ui.call("project.add", str(root))


def state(ui: HeadlessUI) -> dict[str, Any]:
    """What the frame shows: the project, and each window's buffer and cursor."""
    return value(
        ui,
        "{'project': _f.workspace.name, "
        "'windows': [(w.buffer.name, w.cursor.pos) for w in _f.windows()], "
        "'focus': _f.focus.buffer.name, 'panels': sorted(_f.panels)}",
    )


def names(ui: HeadlessUI) -> list[str]:
    return value(ui, "[w.name for w in ed.workspaces]")


# -- the list ---------------------------------------------------------------------------


def test_a_fresh_editor_shows_no_project(ui: HeadlessUI) -> None:
    assert names(ui) == ["No project"]
    assert state(ui)["project"] == "No project"
    assert value(ui, "(ed.workspaces.none.root, ed.workspaces.none.removable)") == (None, False)


def test_adding_renaming_reordering_and_removing(ui: HeadlessUI, tmp_path: Path) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt"), tree(tmp_path / "beta", "b.txt")
    other = tree(tmp_path / "elsewhere" / "alpha", "c.txt")
    add(ui, alpha)
    assert ui.message == f"Project alpha: {alpha.resolve()}" and state(ui)["project"] == "alpha"
    add(ui, beta)
    add(ui, other)  # the same directory name: told apart
    add(ui, alpha)  # the same directory: the project it already is
    assert names(ui) == ["alpha", "beta", "alpha (2)", "No project"]
    assert state(ui)["project"] == "alpha"
    ui.eval("ed.workspaces.rename(ed.workspaces.get('alpha (2)'), 'gamma')")
    ui.eval("ed.workspaces.move(ed.workspaces.get('gamma'), 0)")
    assert names(ui) == ["gamma", "alpha", "beta", "No project"]
    ui.keys("C-c", "p", "n")
    assert state(ui)["project"] == "beta"
    ui.keys("C-c", "p", "n", "C-c", "p", "n")
    assert state(ui)["project"] == "gamma"  # round, by way of "No project"
    ui.keys("C-c", "p", "b")
    assert state(ui)["project"] == "No project"
    for bad, complaint in [
        ("ed.workspaces.add('/no/such/directory')", "is not a directory"),
        ("ed.workspaces.rename(ed.workspaces.none, 'x')", "cannot be renamed"),
        ("ed.workspaces.remove(ed.workspaces.none)", "cannot be removed"),
        ("ed.workspaces.rename(ed.workspaces.get('beta'), '  ')", "needs a name"),
    ]:
        with pytest.raises(Exception, match=complaint):
            ui.client.eval(bad)


# -- where a buffer belongs ---------------------------------------------------------------


def test_a_buffer_belongs_to_the_nearest_root_or_to_where_it_was_opened(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    outer = tree(tmp_path / "outer", "top.txt", "vendor/inner/deep.txt")
    inner = outer / "vendor" / "inner"
    loose = tree(tmp_path / "loose", "notes.txt")
    add(ui, outer)
    add(ui, inner)
    owner = "ed.workspaces.of(ed.buffers.visit(%r)).name"
    assert value(ui, owner % str(outer / "top.txt")) == "outer"
    assert value(ui, owner % str(inner / "deep.txt")) == "inner"  # the nearest root wins
    # Under no root: it belongs to the project it was opened in, here "inner".
    ui.open(loose / "notes.txt")
    assert state(ui)["project"] == "inner"
    assert value(ui, "ed.workspaces.of(ed.buffer).name") == "inner"
    # So does a buffer with no file.  The editor's own belong to none.
    ui.eval("_n = ed.buffers.new('untitled'); _m = ed.buffers.get('*messages*')")
    assert value(ui, "(ed.workspaces.of(_n).name, ed.workspaces.of(_m))") == ("inner", None)
    # Saved under a new name, a buffer is where its file now is.
    ui.eval(f"_n.save({str(outer / 'kept.txt')!r})")
    assert value(ui, "ed.workspaces.of(_n).name") == "outer"


def test_adding_a_project_takes_in_the_files_already_open_under_it(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    proj = tree(tmp_path / "proj", "a.txt")
    ui.open(proj / "a.txt")
    assert state(ui) == {
        "project": "No project", "windows": [("a.txt", 0)], "focus": "a.txt", "panels": [],
    }  # fmt: skip
    add(ui, proj)
    assert state(ui)["project"] == "proj" and state(ui)["windows"] == [("a.txt", 0)]
    assert value(ui, "ed.workspaces.of(ed.buffers.get('a.txt')).name") == "proj"
    ui.keys("C-c", "p", "n")
    # The window it was in before, in the project it is no longer in, shows something else.
    assert state(ui) == {
        "project": "No project", "windows": [("*scratch*", 0)], "focus": "*scratch*", "panels": [],
    }  # fmt: skip


# -- switching ---------------------------------------------------------------------------


def test_each_project_keeps_its_own_windows_cursors_and_panels(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha = tree(tmp_path / "alpha", "one.txt", "two.txt")
    beta = tree(tmp_path / "beta", "b.py")
    add(ui, alpha)
    ui.open(alpha / "one.txt")
    ui.keys("C-e")
    ui.keys("C-x", "2")
    ui.open(alpha / "two.txt")
    ui.keys("C-f", "C-f")
    ui.eval("ed.show(ed.buffers.get('*messages*'), 'messages')")  # a panel, in this project only
    in_alpha = state(ui)
    assert in_alpha["windows"] == [("one.txt", 7), ("two.txt", 2)]
    assert in_alpha["panels"] == ["bottom"] and in_alpha["focus"] == "two.txt"
    shown = sorted(v.info["name"] for v in ui.model.views.values())

    add(ui, beta)
    assert state(ui) == {
        "project": "beta", "windows": [("*scratch*", 0)], "focus": "*scratch*", "panels": [],
    }  # fmt: skip
    # The frontend was told afresh, and holds nothing of the other project.
    assert sorted(v.info["name"] for v in ui.model.views.values()) == ["*scratch*"]
    assert ui.model.layout == {"main": ["win", ui.view.id], "panels": []}
    ui.open(beta / "b.py")
    ui.type("x = 1")
    assert ui.text_rows_shown()[0] == " 1 x = 1b.py"

    ui.keys("C-c", "p", "p")
    assert ui.prompt == "Switch to project: "
    assert ui.candidates() == ["alpha", "beta", "No project"]
    ui.type("alp")
    ui.keys("<return>")
    assert state(ui) == in_alpha and ui.message == "Project alpha"
    assert sorted(v.info["name"] for v in ui.model.views.values()) == shown
    assert ui.model.layout["main"][0] == "split" and len(ui.model.layout["panels"]) == 1
    # Typing lands where the cursor was left.
    ui.type("!")
    assert value(ui, "ed.buffers.get('two.txt').text") == "tw!o.txt\n"

    ui.keys("C-c", "p", "n")
    assert state(ui)["windows"] == [("b.py", 5)]


def test_unsaved_work_in_a_project_that_is_not_showing_is_kept(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt"), tree(tmp_path / "beta", "b.txt")
    add(ui, alpha)
    ui.open(alpha / "a.txt")
    ui.type("unsaved ")
    add(ui, beta)
    assert value(ui, "(ed.buffers.get('a.txt').text, ed.buffers.get('a.txt').modified)") == (
        "unsaved a.txt\n", True,
    )  # fmt: skip
    assert value(ui, "ed.journal.wanted(ed.buffers.get('a.txt'))") is True
    assert value(ui, "[b.name for b in ed.workspaces.unsaved(ed.workspaces.get('alpha'))]") == [
        "a.txt"
    ]
    # An edit made to it from here is where the cursor in its own project expects it.
    ui.eval("ed.buffers.get('a.txt').insert(0, '>> ')")
    ui.keys("C-c", "p", "b")
    assert state(ui)["windows"] == [("a.txt", 11)]


def test_a_buffer_is_shown_in_its_own_project(ui: HeadlessUI, tmp_path: Path) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt"), tree(tmp_path / "beta", "b.txt")
    add(ui, alpha)
    add(ui, beta)
    ui.open(alpha / "a.txt")  # from beta: opening it goes to alpha
    assert state(ui)["project"] == "alpha" and state(ui)["focus"] == "a.txt"
    ui.open(beta / "b.txt")
    assert state(ui)["project"] == "beta"
    # The buffers offered are this project's and the editor's own.
    ui.keys("C-x", "b")
    offered = ui.candidates()
    assert "b.txt" in offered and "*messages*" in offered and "a.txt" not in offered
    ui.keys("C-g")
    # With a prefix argument, every buffer, each with the project it is in.
    ui.keys("C-u", "C-x", "b")
    assert ui.prompt == "Switch to buffer (any project): "
    ui.type("a.t")
    assert [(i[0], i[1]) for i in ui.model.popup["items"]] == [("a.txt", "alpha")]
    ui.keys("<return>")
    assert state(ui)["project"] == "alpha" and state(ui)["focus"] == "a.txt"
    # One of the editor's own is shown wherever one is.
    ui.keys("C-x", "b")
    ui.type("*messages*")
    ui.keys("<return>")
    assert state(ui)["project"] == "alpha" and state(ui)["focus"] == "*messages*"


def test_a_file_name_is_asked_for_from_the_projects_directory(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha = tree(tmp_path / "alpha", "src/main.py")
    add(ui, alpha)
    ui.keys("C-x", "C-f")
    assert value(ui, "ed.frame.focus.buffer.text") == f"{alpha.resolve()}/"
    ui.type("src/ma")
    assert ui.candidates() == [f"{alpha.resolve()}/src/main.py"]
    ui.keys("<down>", "<return>")
    assert state(ui)["focus"] == "main.py"
    ui.keys("C-c", "p", "n")  # "No project" has no directory of its own
    ui.keys("C-x", "C-f")
    assert value(ui, "ed.frame.focus.buffer.text") == ""
    ui.keys("C-g")


def test_a_prompt_does_not_survive_a_switch(ui: HeadlessUI, tmp_path: Path) -> None:
    add(ui, tree(tmp_path / "alpha", "a.txt"))
    ui.keys("M-x")
    assert ui.prompt == "M-x "
    ui.eval("_f.switch(ed.workspaces.none)")
    assert ui.prompt is None and state(ui)["project"] == "No project"
    ui.type("typed")
    assert value(ui, "ed.buffers.get('*scratch*').text").endswith("typed")


# -- killing and removing ------------------------------------------------------------------


def test_killing_a_buffer_shown_in_a_layout_that_is_not_on_screen(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha = tree(tmp_path / "alpha", "a.txt", "other.txt")
    add(ui, alpha)
    ui.open(alpha / "other.txt")
    ui.open(alpha / "a.txt")
    ui.keys("C-c", "p", "n")
    ui.eval("ed.buffers.kill(ed.buffers.get('a.txt'))")
    ui.keys("C-c", "p", "b")
    assert state(ui)["windows"] == [("other.txt", 0)]  # another of the same project
    ui.keys("C-x", "k")
    assert state(ui)["windows"] == [("*scratch*", 0)]


def test_a_project_with_unsaved_buffers_cannot_be_removed(ui: HeadlessUI, tmp_path: Path) -> None:
    alpha = tree(tmp_path / "alpha", "a.txt", "b.txt")
    add(ui, alpha)
    ui.open(alpha / "b.txt")
    ui.open(alpha / "a.txt")
    ui.type("x")
    ui.keys("M-x")
    ui.type("project.remove")
    ui.keys("<return>")
    ui.type("alpha")
    ui.keys("<return>")
    assert ui.message == "alpha has unsaved buffers (a.txt); save or kill them first"
    assert names(ui) == ["alpha", "No project"]
    ui.keys("C-x", "C-s")
    ui.eval("ed.workspaces.remove(ed.workspaces.get('alpha'))")
    # The frame showing it shows "No project"; its buffers are closed; its files are there.
    assert names(ui) == ["No project"] and state(ui)["project"] == "No project"
    assert value(ui, "[b.name for b in ed.buffers if b.file is not None]") == []
    assert (alpha / "a.txt").read_text() == "xa.txt\n" and (alpha / "b.txt").exists()
    assert value(ui, "ed.workspaces.none.arrangements") == {}


# -- more than one frame ------------------------------------------------------------------


def test_each_frame_has_its_own_project_and_layout(tmp_path: Path) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt"), tree(tmp_path / "beta", "b.txt")
    with running_engine() as path, HeadlessUI(path, rows=10, cols=60) as one:
        with HeadlessUI(path, rows=10, cols=60) as two:
            one.eval(f"_f1 = ed.frames[{one.frame_id}]; _f2 = ed.frames[{two.frame_id}]")
            one.call("project.add", str(alpha))
            one.open(alpha / "a.txt")
            two.call("project.add", str(beta))
            two.open(beta / "b.txt")
            assert value(one, "(_f1.workspace.name, _f2.workspace.name)") == ("alpha", "beta")
            # The second frame comes to alpha with a layout of its own.
            two.call("project.previous")
            two.keys("C-x", "2")
            assert value(one, "(len(_f1.windows()), len(_f2.windows()))") == (1, 2)
            one.type("seen in both ")
            two.idle()
            assert two.text_rows_shown()[0].startswith(" 1 seen in both ")
            two.client.notify("ui.detach")
        one.idle()
        # A frame that has gone leaves no layouts behind.
        kept = value(one, "{w.name: sorted(w.arrangements) for w in ed.workspaces}")
        assert all(frames in ([], [one.frame_id]) for frames in kept.values()), kept


def test_a_shell_command_runs_in_the_projects_directory(ui: HeadlessUI, tmp_path: Path) -> None:
    import time

    alpha = tree(tmp_path / "alpha", "a.txt")
    add(ui, alpha)
    ui.call("shell_command", "pwd")
    out = "ed.buffers.get('*shell output*')"
    deadline = time.monotonic() + 10
    while value(ui, f"{out}.locals['process'].running"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert value(ui, f"{out}.text").split("\n")[1] == str(alpha.resolve())


# -- from review -----------------------------------------------------------------------------


def test_unsaved_text_with_no_file_also_keeps_a_project_from_being_removed(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    add(ui, tree(tmp_path / "alpha", "a.txt"))
    ui.call("buffers.switch", "notes")
    ui.type("thoughts not saved anywhere")
    with pytest.raises(Exception, match=r"alpha has unsaved buffers \(notes\)"):
        ui.client.eval("ed.workspaces.remove(ed.workspaces.get('alpha'))")
    assert value(ui, "ed.buffers.get('notes').text") == "thoughts not saved anywhere"
    assert names(ui) == ["alpha", "No project"]


def test_saving_a_buffer_into_another_project_takes_it_and_the_frame_there(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt", "stays.txt"), tree(tmp_path / "beta", "b.txt")
    add(ui, beta)
    add(ui, alpha)
    ui.open(alpha / "stays.txt")
    ui.open(alpha / "a.txt")
    ui.keys("C-x", "2")  # shown in two windows of alpha
    ui.eval(f"ed.buffer.save({str(beta / 'moved.txt')!r})")
    # It is beta's now, and the frame that was working in it has gone with it.
    assert value(ui, "ed.workspaces.of(ed.buffers.get('a.txt')).name") == "beta"
    assert state(ui)["project"] == "beta" and state(ui)["focus"] == "a.txt"
    # Alpha's windows show something of alpha's.
    ui.keys("C-c", "p", "n")
    assert state(ui)["windows"] == [("stays.txt", 0), ("stays.txt", 0)]
    # Removing beta kills the buffer, and nothing anywhere is left showing it.
    ui.eval("ed.workspaces.remove(ed.workspaces.get('beta'))")
    assert value(ui, "ed.buffers.get('a.txt')") is None
    shown = value(ui, "[v.buffer.name for f in ed.frames.values() for v in f.views.values()]")
    assert shown == ["stays.txt", "stays.txt"]
    assert value(
        ui, "all(v.buffer.name in ed.buffers for f in ed.frames.values() for v in f.views.values())"
    )


def test_a_command_that_switches_projects_keeps_the_buffer_it_started_in(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt"), tree(tmp_path / "beta", "b.txt")
    add(ui, beta)
    ui.open(beta / "b.txt")
    add(ui, alpha)
    ui.open(alpha / "a.txt")
    ui.eval(
        "seen = []\n"
        "@command\n"
        "def hop():\n"
        "    seen.append(ed.buffer.name)\n"
        "    ed.frame.switch(ed.workspaces.get('beta'))\n"
        "    seen.append((ed.buffer.name, ed.view.buffer.name, ed.window.buffer.name))\n"
        "    ed.buffer.insert(0, 'from the command: ')\n"
    )
    ui.call("hop")
    assert value(ui, "seen") == ["a.txt", ("a.txt", "a.txt", "a.txt")]
    assert value(ui, "ed.buffers.get('a.txt').text") == "from the command: a.txt\n"
    assert value(ui, "ed.buffers.get('b.txt').text") == "b.txt\n"
    assert state(ui)["project"] == "beta"
    # Outside the command there is no pin: the frame's focused view is what is meant.
    assert value(ui, "ed.buffer.name") == "b.txt"
    # And the edit is one undo step in the buffer it was made in.
    ui.keys("C-c", "p", "n")
    ui.keys("C-/")
    assert value(ui, "ed.buffers.get('a.txt').text") == "a.txt\n"


def test_killing_the_scratch_buffer_does_not_leave_it_in_a_layout_put_away(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    (tmp_path / "empty").mkdir()
    add(ui, tmp_path / "empty")
    # Both projects' windows show *scratch*: this one's, and the one put away.
    assert state(ui) == {
        "project": "empty", "windows": [("*scratch*", 0)], "focus": "*scratch*", "panels": [],
    }  # fmt: skip
    ui.keys("C-x", "k")
    here = state(ui)["windows"][0][0]
    assert here.startswith("*scratch*") and here in value(ui, "[b.name for b in ed.buffers]")
    ui.keys("C-c", "p", "n")
    assert state(ui)["project"] == "No project"
    there = state(ui)["windows"][0][0]
    # A buffer that is registered, not the one that was killed.
    assert there in value(ui, "[b.name for b in ed.buffers]")
    assert value(ui, "ed.buffers.get(_f.window.buffer.name) is _f.window.buffer") is True
    ui.type("usable")
    assert value(ui, "_f.window.buffer.text") == "usable"


def test_a_panel_tab_put_away_is_closed_when_its_buffer_is_killed(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha = tree(tmp_path / "alpha", "a.txt")
    add(ui, alpha)
    ui.open(alpha / "a.txt")
    ui.eval(
        "_out = ed.buffers.new('*output of something*'); _out.insert(0, 'output')\n"
        "ed.show(_out, 'output'); ed.show(ed.buffers.get('*messages*'), 'messages')\n"
        "ed.show(_out, 'output', focus=True)"
    )
    assert state(ui)["focus"] == "*output of something*"
    ui.keys("C-c", "p", "n")  # alpha is put away with the keyboard in that tab
    ui.eval("ed.buffers.kill(_out)")
    ui.keys("C-c", "p", "b")
    # The tab has gone, as it would have were it on screen; no file took its place.
    tabs = value(ui, "[t.buffer.name for t in _f.panels['bottom'].tabs]")
    assert tabs == ["*messages*"]
    assert state(ui)["windows"] == [("a.txt", 0)] and state(ui)["focus"] == "*messages*"
    assert value(ui, "all(v.buffer.name in ed.buffers for v in _f.views.values())")


def test_buffer_choices_are_remembered(ui: HeadlessUI) -> None:
    ui.eval("ed.buffers.new('first'); ed.buffers.new('second')")
    for name in ("first", "second"):
        ui.keys("C-x", "b")
        ui.type(name)
        ui.keys("<return>")
    ui.keys("C-x", "b", "M-p", "M-p")
    assert value(ui, "ed.frame.focus.buffer.text") == "first"
    ui.keys("C-g")


def test_a_command_that_saves_into_another_project_goes_on_with_the_same_buffer(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    alpha, beta = tree(tmp_path / "alpha", "a.txt", "stays.txt"), tree(tmp_path / "beta", "b.txt")
    add(ui, beta)
    add(ui, alpha)
    ui.open(alpha / "stays.txt")
    ui.open(alpha / "a.txt")
    ui.keys("C-f", "C-<space>", "C-f", "C-f")  # a selection, to be kept
    before = value(ui, "(_f.focus.selections.ranges(), _f.focus.buffer.name)")
    assert before == ([(1, 3)], "a.txt")
    target = beta / "moved.txt"
    ui.eval(
        "seen = []\n"
        "@command\n"
        f"def save_elsewhere():\n"
        f"    ed.buffer.save({str(target)!r})\n"
        "    seen.append((ed.buffer.name, str(ed.buffer.file), ed.view.buffer.name))\n"
        "    ed.buffer.insert(len(ed.buffer), 'added by the command after saving\\n')\n"
    )
    ui.call("save_elsewhere")
    # The command went on with the buffer it saved, not the one that took its place.
    assert value(ui, "seen") == [("a.txt", str(target.resolve()), "a.txt")]
    assert value(ui, "ed.buffers.get('a.txt').text").endswith("added by the command after saving\n")
    assert value(ui, "ed.buffers.get('stays.txt').text") == "stays.txt\n"
    # It is shown in its new project with the selection it had.
    assert state(ui)["project"] == "beta" and state(ui)["focus"] == "a.txt"
    assert value(ui, "_f.focus.selections.ranges()") == [(1, 3)]
    # The command that does this for the user says where it went.
    ui.call("files.save_as", str(alpha / "back.txt"))
    assert ui.message == f"Wrote {(alpha / 'back.txt').resolve()}"
    assert state(ui)["project"] == "alpha"
