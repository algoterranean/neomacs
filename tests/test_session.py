"""Session restore (WORKSPACE.md, stage W-6): what was open comes back when
the engine is started again on the same data directory."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI

WORKBENCH = {"capabilities": ["panels", "workbench"]}


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def wait_for(condition: Callable[[], bool], what: str = "it", timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen")
        time.sleep(0.02)


@contextmanager
def engine(
    data: Path, *, attach: dict[str, Any] | None = None, env: dict[str, str] | None = None
) -> Iterator[HeadlessUI]:
    """An engine keeping its session under DATA, with a frontend attached."""
    settings = {"NEOMACS_DATA_DIR": str(data), **(env or {})}
    with running_engine(env=settings) as path:
        with HeadlessUI(path, rows=14, cols=90, attach=attach or WORKBENCH) as ui:
            ui.eval(f"_f = ed.frames[{ui.frame_id}]; ed.options.session_delay = 0.05")
            yield ui


def stop(ui: HeadlessUI) -> None:
    """Stop the engine as the user does, and wait until it has gone."""
    pid = value(ui, "__import__('os').getpid()")
    ui.client.notify("engine.call", "engine.exit", [])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        # It is this process's child: once it has exited it is listed as a
        # zombie until it is waited for, which is the context manager's to do.
        listed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not listed or listed.startswith("Z"):
            return
        time.sleep(0.02)
    raise AssertionError("the engine did not stop")


def saved(data: Path) -> dict[str, Any]:
    return json.loads((data / "session.json").read_text())


def state(ui: HeadlessUI) -> dict[str, Any]:
    """What the frame shows.  A buffer goes by its file's name: two files of
    one name in two projects are told apart in buffer names by the order
    they were opened in, which is not what is being kept."""
    ui.eval("_n = lambda b: b.file.name if b.file else b.name")
    return value(
        ui,
        "{'project': _f.workspace.name, 'projects': [w.name for w in ed.workspaces], "
        "'windows': [([_n(b) for b in w.tabs], _n(w.buffer), "
        "w.buffer.line_offset(w.cursor.pos)) for w in _f.windows()], "
        "'focus': _n(_f.focus.buffer)}",
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for project in ("alpha", "beta"):
        for name in ("one.txt", "two.txt", "three.txt"):
            file = tmp_path / project / name
            file.parent.mkdir(exist_ok=True)
            file.write_text("".join(f"{project} {name} line {n}\n" for n in range(1, 31)))
    return tmp_path.resolve()


def lay_out(ui: HeadlessUI, tree: Path) -> None:
    """Two projects: alpha with two windows side by side, beta with one."""
    ui.call("project.add", str(tree / "alpha"))
    ui.open(tree / "alpha" / "one.txt")
    ui.open(tree / "alpha" / "two.txt")
    ui.eval("_f.window.selections.set([(_f.window.buffer.position(7, 3),) * 2])")
    ui.keys("C-x", "3")
    ui.open(tree / "alpha" / "three.txt")
    ui.eval("_f.window.selections.set([(_f.window.buffer.position(20, 0),) * 2])")
    ui.call("project.add", str(tree / "beta"))
    ui.open(tree / "beta" / "one.txt")
    ui.eval("_f.window.selections.set([(_f.window.buffer.position(3, 5),) * 2])")
    ui.call("project.switch", "alpha")


ALPHA = [
    (["one.txt", "two.txt"], "two.txt", (7, 3)),
    (["two.txt", "three.txt"], "three.txt", (20, 0)),
]


def test_projects_files_windows_tabs_and_cursors_come_back(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        lay_out(ui, tree)
        before = state(ui)
        assert before["windows"] == ALPHA and before["project"] == "alpha"
        ui.eval("_f.root.ratios = [0.3, 0.7]; _f.schedule_update()")
        ui.client.notify("ui.region", "details", None, 23.5)
        ui.call("view.toggle_project_sidebar")
        stop(ui)
    with engine(data) as ui:
        after = state(ui)
        assert after == before
        assert after["projects"] == ["alpha", "beta", "No project"]
        assert value(ui, "[round(r, 2) for r in _f.root.ratios]") == [0.3, 0.7]
        assert ui.model.regions["details"]["width"] == 23.5
        assert ui.model.regions["project"]["shown"] is False
        # The frontend was told of it as of any layout: two windows, with their tabs.
        assert [tab["title"] for tab in ui.view.info["tabs"]] == ["two.txt", "three.txt"]
        # A tab that was not showing has the cursor it had: the split gave
        # the new window the place the old one was at.
        ui.keys("s-{")
        assert state(ui)["windows"][1] == (["two.txt", "three.txt"], "two.txt", (7, 3))
        ui.eval("_f.focus_view(_f.windows()[0])")
        ui.keys("s-{")
        assert state(ui)["windows"][0] == (["one.txt", "two.txt"], "one.txt", (1, 0))
        # The project that was not showing has its layout too.
        ui.call("project.switch", "beta")
        assert state(ui)["windows"] == [(["one.txt"], "one.txt", (3, 5))]
        # Nothing of this was text: every buffer is as its file is.
        assert value(ui, "[b.name for b in ed.buffers if b.modified]") == []


def test_it_is_written_as_things_change_not_only_at_the_end(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        lay_out(ui, tree)
        wait_for(lambda: (data / "session.json").exists(), "the session being written")
        wait_for(
            lambda: [p["name"] for p in saved(data)["projects"]] == ["alpha", "beta", "No project"]
        )
        wait_for(lambda: saved(data)["frames"][0]["current"] == 0, "the layout being written")
        alpha = saved(data)["frames"][0]["arrangements"]["0"]
        assert alpha["tree"]["split"] == "h" and len(alpha["tree"]["children"]) == 2
        assert alpha["tree"]["children"][1]["active"] == str(tree / "alpha" / "three.txt")
        assert "text" not in json.dumps(saved(data))  # no text is kept, only where things are
        assert (data / "session.json").stat().st_mode & 0o777 == 0o600
        # Killed outright, with no chance to write: what was last written stands.
        os.kill(value(ui, "__import__('os').getpid()"), signal.SIGKILL)
        time.sleep(0.2)
    with engine(data) as ui:
        assert state(ui)["windows"] == ALPHA


def test_what_has_gone_is_left_out_and_named_once(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        lay_out(ui, tree)
        stop(ui)
    (tree / "alpha" / "three.txt").unlink()
    for file in (tree / "beta").iterdir():
        file.unlink()
    (tree / "beta").rmdir()
    with engine(data) as ui:
        now = state(ui)
        assert now["projects"] == ["alpha", "No project"]
        # The tab whose file went is not made; the window shows the one it has left.
        assert now["windows"] == [
            (["one.txt", "two.txt"], "two.txt", (7, 3)),
            (["two.txt"], "two.txt", (7, 3)),
        ]
        assert ui.message.startswith("No longer there, and not opened again: ")
        assert str(tree / "beta") in ui.message and str(tree / "alpha" / "three.txt") in ui.message


def test_the_option_turns_it_off_and_safe_does_not(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    config = tmp_path / "config"
    config.mkdir()
    with engine(data) as ui:
        lay_out(ui, tree)
        stop(ui)
    (config / "init.py").write_text("from neomacs import ed\ned.options.session_restore = False\n")
    with engine(data, env={"NEOMACS_CONFIG_DIR": str(config)}) as ui:
        assert state(ui)["projects"] == ["No project"]
        ui.eval("ed.session._closed = True")  # leave what is written as it is
        os.kill(value(ui, "__import__('os').getpid()"), signal.SIGKILL)
        time.sleep(0.2)
    # --safe runs none of the user's code; a session is not code.
    with running_engine(env={"NEOMACS_DATA_DIR": str(data)}, args=["--safe"]) as path:
        with HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as ui:
            ui.eval(f"_f = ed.frames[{ui.frame_id}]")
            assert state(ui)["windows"] == ALPHA


def test_a_frontend_with_one_window_gets_the_one_that_had_the_keyboard(
    tmp_path: Path, tree: Path
) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        lay_out(ui, tree)
        stop(ui)
    with engine(data, attach={"capabilities": []}) as ui:
        assert state(ui)["windows"] == [(["two.txt", "three.txt"], "three.txt", (20, 0))]


def test_each_frame_gets_its_own_layout_back(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with running_engine(env={"NEOMACS_DATA_DIR": str(data)}) as path:
        with (
            HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as first,
            HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as second,
        ):
            first.eval(f"_f = ed.frames[{first.frame_id}]")
            lay_out(first, tree)
            second.eval(f"_g = ed.frames[{second.frame_id}]")
            second.call("project.switch", "beta")
            second.open(tree / "beta" / "two.txt")
            stop(first)
    with running_engine(env={"NEOMACS_DATA_DIR": str(data)}) as path:
        with (
            HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as first,
            HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as second,
            HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as third,
        ):
            first.eval(f"_f = ed.frames[{first.frame_id}]")
            assert state(first)["windows"] == ALPHA
            second.eval(f"_f = ed.frames[{second.frame_id}]")
            assert state(second)["project"] == "beta"
            assert state(second)["windows"] == [(["one.txt", "two.txt"], "two.txt", (1, 0))]
            # A third had nothing kept for it: it starts as any new frame does.
            third.eval(f"_f = ed.frames[{third.frame_id}]")
            assert state(third)["windows"] == [(["*scratch*"], "*scratch*", (1, 0))]


def test_the_folders_open_in_the_file_tree_are_kept(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    (tree / "alpha" / "src" / "deep").mkdir(parents=True)
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        workspace = value(ui, "_f.workspace.id")
        ui.client.notify("ui.folders", workspace, ["src", "src/deep", "../outside", "/etc"])
        ui.idle()
        assert ui.model.workspaces[0]["folders"] == ["src", "src/deep"]  # only its own
        stop(ui)
    with engine(data) as ui:
        assert ui.model.workspaces[0]["folders"] == ["src", "src/deep"]


def test_a_damaged_file_is_passed_over(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for text in ("not json at all", '{"format": 99, "projects": 3}', '{"format": 1, "projects": 3}',
                 '{"format": 1, "projects": [], "frames": [{"current": 5, "arrangements": {"9": 1}}]}'):  # fmt: skip
        (data / "session.json").write_text(text)
        with engine(data) as ui:
            assert state(ui)["projects"] == ["No project"]
            assert state(ui)["windows"] == [(["*scratch*"], "*scratch*", (1, 0))]
            stop(ui)


# -- faults found in review --------------------------------------------------------------


def test_a_file_outside_every_project_comes_back_in_the_one_it_was_opened_in(
    tmp_path: Path, tree: Path
) -> None:
    data = tmp_path / "data"
    stray = tmp_path / "elsewhere" / "stray.txt"
    stray.parent.mkdir()
    stray.write_text("stray\n")
    owner = "ed.workspaces.of(ed.buffers.get('stray.txt')).name"
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        ui.eval(f"_f.window.show(ed.buffers.visit({str(stray)!r}))")
        assert value(ui, owner) == "alpha"
        stop(ui)
    with engine(data) as ui:
        assert value(ui, owner) == "alpha"
        assert state(ui)["windows"] == [(["stray.txt"], "stray.txt", (1, 0))]
        stop(ui)
    with engine(data) as ui:  # and it is written back as alpha's, not as nobody's
        assert value(ui, owner) == "alpha"


def test_the_keyboard_goes_back_to_its_window_though_another_has_gone(
    tmp_path: Path, tree: Path
) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        for name in ("one.txt", "two.txt", "three.txt"):
            ui.open(tree / "alpha" / name)
            ui.eval("_f.window.tabs[:] = [_f.window.buffer]")
            if name != "three.txt":
                ui.keys("C-x", "3")
        ui.eval("_f.focus_view(_f.windows()[1])")
        assert [w[1] for w in state(ui)["windows"]] == ["one.txt", "two.txt", "three.txt"]
        assert state(ui)["focus"] == "two.txt"
        stop(ui)
    (tree / "alpha" / "one.txt").unlink()  # the first window has nothing left to show
    with engine(data) as ui:
        now = state(ui)
        assert [w[1] for w in now["windows"]] == ["two.txt", "three.txt"]
        assert now["focus"] == "two.txt"  # not the window that now has its place
    (
        tree / "alpha" / "two.txt"
    ).unlink()  # the one that had the keyboard: the first is the fallback
    with engine(data) as ui:
        assert state(ui)["focus"] == "three.txt"


def test_a_restored_cursor_is_in_view(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        ui.open(tree / "alpha" / "one.txt")
        ui.eval("_f.window.selections.set([(_f.window.buffer.position(28, 2),) * 2])")
        stop(ui)
    with engine(data) as ui:
        assert state(ui)["windows"] == [(["one.txt"], "one.txt", (28, 2))]
        first, last, _ = value(ui, "_f.window.viewport")
        assert first > 1 and first <= 28 <= last  # one window, one tab: still brought into view


def test_a_write_decided_earlier_does_not_replace_a_later_one(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        wait_for(lambda: (data / "session.json").exists(), "the session being written")
        ui.eval(
            "_s = ed.session; _s._closed = True\n"
            "_early, _late = next(_s._order), next(_s._order)\n"
            "_s._replace('the later state', _late)\n_s._replace('an earlier state', _early)"
        )
        assert (data / "session.json").read_text() == "the later state"


def test_tabs_do_not_open_more_files_than_the_limit(tmp_path: Path, tree: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        path = str(tree / "alpha" / "one.txt")
        ui.eval("ed.session._visits_left = 1")
        assert value(ui, f"ed.session._buffer({path!r}).name") == "one.txt"
        other = str(tree / "alpha" / "two.txt")
        assert value(ui, f"ed.session._buffer({other!r})") is None  # none left to make
        assert value(ui, f"ed.session._buffer({path!r}).name") == "one.txt"  # open already
        assert value(ui, "ed.session.missing") == []  # it is there; it was only not opened


def test_the_tab_that_is_showing_is_kept_when_tabs_are_over_the_limit(
    tmp_path: Path, tree: Path
) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.call("project.add", str(tree / "alpha"))
        for name in ("one.txt", "two.txt", "three.txt"):
            ui.open(tree / "alpha" / name)
        ui.open(tree / "alpha" / "one.txt")  # the first tab is the one showing
        ui.eval("import neomacs.session as _sm; _sm.MAX_FILES = 2")
        kept = value(ui, "ed.session.take()['frames'][0]['arrangements']['0']['tree']")
        assert [Path(tab[0]).name for tab in kept["tabs"]] == ["one.txt", "three.txt"]
        assert Path(kept["active"]).name == "one.txt"
        ui.open(tree / "alpha" / "three.txt")  # one of the latest: nothing special to do
        kept = value(ui, "ed.session.take()['frames'][0]['arrangements']['0']['tree']")
        assert [Path(tab[0]).name for tab in kept["tabs"]] == ["two.txt", "three.txt"]
