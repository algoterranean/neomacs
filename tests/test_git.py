"""Git (WORKSPACE.md, stage W-5): what the git panel is told, the commands
that stage, discard and commit, and the side-by-side diff.  Every repository
here is made with real git in a temporary directory."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI

WORKBENCH = {"capabilities": ["panels", "workbench"]}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}  # fmt: skip


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def run_git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )  # fmt: skip
    return done.stdout


def wait_for(condition: Callable[[], bool], what: str = "it", timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen")
        time.sleep(0.02)


@pytest.fixture
def trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "the-trash"
    monkeypatch.setenv("NEOMACS_TRASH_DIR", str(where))
    return where


@pytest.fixture
def ui(trash: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HeadlessUI]:
    for name, setting in GIT_ENV.items():  # the engine's git commits as the tests' does
        monkeypatch.setenv(name, setting)
    with running_engine() as path, HeadlessUI(path, rows=14, cols=90, attach=WORKBENCH) as session:
        session.eval(f"_f = ed.frames[{session.frame_id}]")
        yield session


NUMBERS = "".join(f"line {n}\n" for n in range(1, 201))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    for name, text in (("a.txt", "alpha\n"), ("b.txt", "one\ntwo\nthree\nfour\nfive\n"),
                       ("c.txt", "gamma\n"), ("long.txt", NUMBERS)):  # fmt: skip
        (root / name).write_text(text)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", ".")
    run_git(root, "commit", "-q", "-m", "first")
    return root


def add(ui: HeadlessUI, root: Path) -> None:
    ui.call("project.add", str(root))


def told(ui: HeadlessUI) -> dict[str, Any]:
    """The repository as the frontend was last told, its lists as plain pairs."""
    git = ui.model.git
    assert git is not None
    pairs = {
        name: [(row["path"], row["state"]) for row in git[name]]
        for name in ("staged", "changes", "untracked", "conflicted")
    }
    return {"branch": git["branch"], "ahead": git["ahead"], "behind": git["behind"], **pairs}


def settled(ui: HeadlessUI, **want: Any) -> dict[str, Any]:
    """Wait until the frontend has been told a repository with these lists."""

    def there() -> bool:
        ui.idle()
        return ui.model.git is not None and all(told(ui)[k] == v for k, v in want.items())

    try:
        wait_for(there)
    except AssertionError:
        raise AssertionError(f"wanted {want}, was told {ui.model.git}") from None
    return told(ui)


def answer(ui: HeadlessUI, command: str, args: list[Any], text: str) -> str:
    ui.client.notify("engine.call", command, args)
    wait_for(lambda: (ui.idle(), ui.prompt is not None)[1], f"{command} asking")
    asked = ui.prompt
    assert asked is not None
    ui.type(text)
    ui.keys("<return>")
    return asked


# -- what the panel is told ----------------------------------------------------------


def test_the_frontend_is_told_what_git_says(ui: HeadlessUI, repo: Path, tmp_path: Path) -> None:
    (repo / "a.txt").write_text("alpha, changed\n")
    (repo / "c.txt").write_text("gamma, staged\n")
    run_git(repo, "add", "c.txt")
    (repo / "c.txt").write_text("gamma, staged, and changed again\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "b.txt").unlink()
    add(ui, repo)
    assert settled(ui, staged=[("c.txt", "modified")]) == {
        "branch": "main", "ahead": 0, "behind": 0,
        "staged": [("c.txt", "modified")],
        "changes": [("a.txt", "modified"), ("b.txt", "deleted"), ("c.txt", "modified")],
        "untracked": [("new.txt", "untracked")],
        "conflicted": [],
    }  # fmt: skip
    assert ui.model.git is not None and ui.model.git["top"] == str(repo)
    # A project that is not in a repository: told so, with nothing.
    plain = tmp_path / "plain"
    plain.mkdir()
    add(ui, plain)
    wait_for(lambda: (ui.idle(), ui.model.git is None)[1], "being told there is no repository")
    ui.call("project.switch", "repo")
    settled(ui, untracked=[("new.txt", "untracked")])


def test_ahead_and_behind_are_told_and_shown_in_the_status_bar(
    ui: HeadlessUI, repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "fetch", "-q", "origin")
    run_git(repo, "branch", "-q", "--set-upstream-to=origin/main")
    (repo / "a.txt").write_text("alpha, again\n")
    run_git(repo, "commit", "-q", "-am", "second")
    add(ui, repo)
    assert settled(ui, ahead=1)["behind"] == 0
    wait_for(
        lambda: (ui.idle(), ui.model.status[0][1]["text"].endswith("main ↑1"))[1],
        "the status bar showing it",
    )


def test_a_conflict_is_told_as_one(ui: HeadlessUI, repo: Path) -> None:
    run_git(repo, "checkout", "-q", "-b", "other")
    (repo / "a.txt").write_text("theirs\n")
    run_git(repo, "commit", "-q", "-am", "theirs")
    run_git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("ours\n")
    run_git(repo, "commit", "-q", "-am", "ours")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "other"],
        capture_output=True, env={**os.environ, **GIT_ENV}, check=False,
    )  # fmt: skip
    add(ui, repo)
    assert settled(ui, conflicted=[("a.txt", "conflicted")])["staged"] == []


# -- staging, discarding, committing ---------------------------------------------------


def test_staging_and_unstaging(ui: HeadlessUI, repo: Path) -> None:
    (repo / "a.txt").write_text("alpha, changed\n")
    (repo / "new.txt").write_text("new\n")
    add(ui, repo)
    settled(ui, changes=[("a.txt", "modified")])
    ui.call("git.stage", "a.txt")
    assert ui.message == "Staged a.txt"
    settled(ui, staged=[("a.txt", "modified")], changes=[])
    ui.call("git.unstage", "a.txt")
    settled(ui, staged=[], changes=[("a.txt", "modified")])
    ui.call("git.stage_all")
    settled(ui, staged=[("a.txt", "modified"), ("new.txt", "added")], untracked=[])
    ui.call("git.unstage_all")
    settled(ui, staged=[], untracked=[("new.txt", "untracked")])
    # By name, with no path: the buffer's file.  A full path is taken too.
    ui.open(repo / "a.txt")
    ui.call("git.stage")
    settled(ui, staged=[("a.txt", "modified")])
    ui.call("git.stage", str(repo / "new.txt"))
    settled(ui, staged=[("a.txt", "modified"), ("new.txt", "added")])
    ui.call("git.stage", "no-such-file.txt")
    assert ui.message.startswith("git: ")


def test_discarding_asks_first(ui: HeadlessUI, repo: Path, trash: Path) -> None:
    (repo / "a.txt").write_text("alpha, changed\n")
    (repo / "new.txt").write_text("new\n")
    add(ui, repo)
    ui.open(repo / "a.txt")
    settled(ui, changes=[("a.txt", "modified")])
    asked = answer(ui, "git.discard", ["a.txt"], "n")
    assert asked == "Discard the changes to a.txt? They cannot be got back. (y or n) "
    assert (repo / "a.txt").read_text() == "alpha, changed\n" and "Nothing" in ui.message
    answer(ui, "git.discard", ["a.txt"], "y")
    assert (repo / "a.txt").read_text() == "alpha\n"
    # The buffer that was showing the file shows what the file is now.
    assert value(ui, "(ed.buffers.get('a.txt').text, ed.buffers.get('a.txt').modified)") == (
        "alpha\n", False,
    )  # fmt: skip
    settled(ui, changes=[])
    # A file git has never seen goes to the trash.
    asked = answer(ui, "git.discard", ["new.txt"], "y")
    assert asked == "Move new.txt to the trash? (y or n) "
    assert not (repo / "new.txt").exists() and (trash / "new.txt").read_text() == "new\n"
    settled(ui, untracked=[])
    ui.call("git.discard", "c.txt")
    assert ui.message == "c.txt has no changes to discard"


def test_committing(ui: HeadlessUI, repo: Path) -> None:
    add(ui, repo)
    ui.call("git.commit", "nothing to commit")
    assert ui.message == "Nothing is staged"
    (repo / "a.txt").write_text("alpha, changed\n")
    (repo / "c.txt").write_text("gamma, changed\n")
    ui.call("git.stage", "a.txt")
    settled(ui, staged=[("a.txt", "modified")])
    ui.call("git.commit", "Change alpha\n\nWith a body.")
    assert ui.message == "Committed: Change alpha"
    assert run_git(repo, "log", "-1", "--format=%B").strip() == "Change alpha\n\nWith a body."
    assert run_git(repo, "show", "--stat", "--format=", "HEAD").split()[0] == "a.txt"
    settled(ui, staged=[], changes=[("c.txt", "modified")])  # what was not staged is still there
    # Asked for when none is given.
    ui.call("git.stage", "c.txt")
    ui.client.notify("engine.call", "git.commit", [])
    wait_for(lambda: (ui.idle(), ui.prompt is not None)[1], "the message being asked for")
    assert (ui.prompt or "").startswith("Commit message (C-Return commits): ")
    ui.type("Change gamma")
    ui.keys("C-<return>")
    wait_for(lambda: run_git(repo, "log", "-1", "--format=%s").strip() == "Change gamma")
    settled(ui, staged=[], changes=[])


# -- the diff ----------------------------------------------------------------------------


def marks(ui: HeadlessUI, which: str) -> list[tuple[int, str]]:
    """(line, face) of every marked line of a side of the diff."""
    return value(
        ui,
        f"sorted((s.buffer.line_offset(s.start)[0] if hasattr(s, 'buffer') else "
        f"_f.diff.{which}_buffer.line_offset(s.start)[0], s.props['line']['face']) "
        f"for s in _f.diff.{which}_buffer.layer(_f.diff.layer))",
    )


def windows(ui: HeadlessUI) -> list[str]:
    return value(ui, "[w.buffer.name for w in _f.windows()]")


def test_a_diff_is_two_windows_with_what_differs_marked(ui: HeadlessUI, repo: Path) -> None:
    (repo / "b.txt").write_text("one\nTWO\nthree\nfive\nsix\nseven\n")
    add(ui, repo)
    ui.open(repo / "a.txt")
    ui.call("git.diff", "b.txt")
    assert windows(ui) == [
        "*b.txt (staged)*",
        "b.txt",
    ]  # what git has at the left, the file at the right
    assert ui.message == "b.txt: staged ↔ file. 3 changes."
    assert value(ui, "(_f.diff.old.buffer.text, _f.diff.old.buffer.read_only)") == (
        "one\ntwo\nthree\nfour\nfive\n", True,
    )  # fmt: skip
    assert value(ui, "(_f.focus is _f.diff.new, _f.diff.new.buffer.read_only)") == (True, False)
    # The marks are the lines `git diff` reports: 2 changed, 4 removed, 5 and 6 added.
    assert marks(ui, "old") == [(2, "diff-changed"), (4, "diff-removed")]
    assert marks(ui, "new") == [(2, "diff-changed"), (5, "diff-added"), (6, "diff-added")]
    hunks = [
        line for line in run_git(repo, "diff", "-U0", "b.txt").splitlines() if line[:2] == "@@"
    ]
    assert [h.split(" ")[1:3] for h in hunks] == [["-2", "+2"], ["-4", "+3,0"], ["-5,0", "+5,2"]]
    # The file's window kept its tabs; the old side has a window to itself.
    assert value(ui, "[[b.name for b in w.tabs] for w in _f.windows()]") == [
        ["*b.txt (staged)*"], ["a.txt", "b.txt"],
    ]  # fmt: skip
    # The frontend is given a face for the whole row of a marked line.
    right = ui.model.view(value(ui, "_f.diff.new.id"))
    assert "face" in right.lines[1][0] and "face" not in right.lines[0][0]


def test_the_marks_follow_the_text(ui: HeadlessUI, repo: Path) -> None:
    (repo / "b.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    add(ui, repo)
    ui.call("git.diff", "b.txt")
    assert marks(ui, "new") == [(2, "diff-changed")]
    ui.eval("_b = _f.diff.new.buffer; _b.replace(_b.line_start(2), _b.line_end(2), 'two')")
    wait_for(lambda: marks(ui, "new") == [], "the marks going")
    ui.eval("_b.insert(len(_b), 'six\\n')")
    wait_for(lambda: marks(ui, "new") == [(6, "diff-added")], "the new line being marked")


def test_stepping_between_changes(ui: HeadlessUI, repo: Path) -> None:
    lines = NUMBERS.split("\n")
    lines[9], lines[99] = "line ten", "line one hundred"
    del lines[149]
    (repo / "long.txt").write_text("\n".join(lines))
    add(ui, repo)
    ui.call("git.diff", "long.txt")
    line = "_f.focus.buffer.line_offset(_f.focus.cursor.pos)[0]"
    assert value(ui, line) == 10  # it opens at the first
    ui.keys("C-c", "g", "n")
    assert (value(ui, line), ui.message) == (100, "Change 2 of 3")
    ui.keys("C-c", "g", "n")
    assert value(ui, line) == 150
    ui.keys("C-c", "g", "n")
    assert ui.message == "No more changes that way"
    ui.keys("C-c", "g", "p")
    assert (value(ui, line), ui.message) == (100, "Change 2 of 3")
    # From the old side too, where the lines are numbered as they were.
    ui.eval("_f.focus_view(_f.diff.old)")
    ui.call("git.next_change")
    assert value(ui, "_f.focus is _f.diff.old") and value(ui, line) == 150


def test_scrolling_one_side_brings_the_other_along(ui: HeadlessUI, repo: Path) -> None:
    lines = NUMBERS.split("\n")
    lines[4:4] = [f"extra {n}" for n in range(1, 21)]  # twenty lines the old side lacks
    (repo / "long.txt").write_text("\n".join(lines))
    add(ui, repo)
    ui.call("git.diff", "long.txt")
    ui.eval("_d = _f.diff")
    assert value(ui, "[_d.matching_line(1, n) for n in (3, 5, 24, 25, 125)]") == [3, 5, 5, 5, 105]
    assert value(ui, "[_d.matching_line(0, n) for n in (3, 5, 105)]") == [3, 25, 125]
    time.sleep(0.35)  # past the moment in which a side is taken to be following
    # The user scrolls the file's side so that line 125 is at its top.
    ui.client.notify("view.viewport", value(ui, "_d.new.id"), 125, 136, 12)
    wait_for(
        lambda: (ui.idle(), value(ui, "_d.old.viewport[0]") == 105)[1], "the old side following"
    )
    # That the old side moved is not taken for the user scrolling it.
    assert value(ui, "_d.new.viewport[0]") == 125


def test_a_staged_diff_is_the_last_commit_against_the_index(ui: HeadlessUI, repo: Path) -> None:
    (repo / "b.txt").write_text("one\ntwo\nthree\nfour\nFIVE\n")
    run_git(repo, "add", "b.txt")
    (repo / "b.txt").write_text("one\ntwo\nthree\nfour\nFIVE\nand not staged\n")
    add(ui, repo)
    ui.call("git.diff", "b.txt", True)
    assert windows(ui) == ["*b.txt (last commit)*", "*b.txt (staged)*"]
    assert value(ui, "_f.diff.new.buffer.text") == "one\ntwo\nthree\nfour\nFIVE\n"
    assert value(ui, "_f.diff.new.buffer.read_only") is True
    assert marks(ui, "new") == [(5, "diff-changed")]
    # Another diff takes its place: the first one's buffers and window go.
    ui.call("git.diff", "b.txt")
    assert windows(ui) == ["*b.txt (staged)*", "b.txt"]
    assert marks(ui, "new") == [(6, "diff-added")]
    assert value(ui, "ed.buffers.get('*b.txt (last commit)*')") is None


def test_closing_either_window_ends_the_diff(ui: HeadlessUI, repo: Path) -> None:
    (repo / "b.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    add(ui, repo)
    ui.open(repo / "a.txt")
    ui.call("git.diff", "b.txt")
    ui.keys("C-x", "0")  # the file's window
    assert value(ui, "_f.diff") is None
    assert value(ui, "ed.buffers.get('*b.txt (staged)*')") is None
    assert (
        value(
            ui,
            "[s for l in ed.buffers.get('b.txt').layers for s in l if l.name.startswith('diff')]",
        )
        == []
    )
    ui.call("git.diff", "b.txt")
    ui.eval("_f.focus_view(_f.diff.old)")
    ui.keys("C-x", "0")  # the old side's
    assert value(ui, "_f.diff") is None and windows(ui)[-1] == "b.txt"
    assert (
        value(
            ui,
            "[s for l in ed.buffers.get('b.txt').layers for s in l if l.name.startswith('diff')]",
        )
        == []
    )
    # The command made for it leaves the file's window as it was.
    ui.open(repo / "a.txt")
    ui.open(repo / "b.txt")
    before = len(windows(ui))
    tabs = value(ui, "[b.name for b in _f.window.tabs]")
    ui.call("git.diff", "b.txt")
    assert len(windows(ui)) == before + 1
    ui.keys("C-c", "g", "q")
    assert value(ui, "_f.diff") is None and len(windows(ui)) == before
    assert value(ui, "(_f.window.buffer.name, [b.name for b in _f.window.tabs])") == (
        "b.txt", tabs,
    )  # fmt: skip
    assert "a.txt" in tabs
    ui.call("git.next_change")
    assert ui.message == "No diff is on show"


def test_what_cannot_be_compared(ui: HeadlessUI, repo: Path, tmp_path: Path) -> None:
    (repo / "new.txt").write_text("new\n")
    add(ui, repo)
    ui.call("git.diff", "new.txt")  # nothing to compare it with: just shown
    assert windows(ui) == ["new.txt"] and "nothing to compare" in ui.message
    ui.call("git.diff", "a.txt")
    assert ui.message == "a.txt: staged ↔ file. The two are the same."
    ui.call("git.close_diff")
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    ui.call("git.diff", str(outside))
    assert "is not in a git repository" in ui.message  # its own directory is asked about


# -- faults found in review ----------------------------------------------------------------


def test_a_name_is_that_file_and_not_a_pattern(ui: HeadlessUI, repo: Path) -> None:
    (repo / "*.txt").write_text("star\n")
    run_git(repo, "add", "--", ":(literal)*.txt")
    run_git(repo, "commit", "-q", "-m", "a file with a star for a name")
    (repo / "*.txt").write_text("star, changed\n")
    (repo / "a.txt").write_text("alpha, changed\n")
    add(ui, repo)
    settled(ui, changes=[("*.txt", "modified"), ("a.txt", "modified")])
    ui.call("git.stage", "*.txt")
    settled(ui, staged=[("*.txt", "modified")], changes=[("a.txt", "modified")])
    ui.call("git.unstage", "*.txt")
    settled(ui, staged=[])
    answer(ui, "git.discard", ["*.txt"], "y")
    assert (repo / "*.txt").read_text() == "star\n"
    assert (repo / "a.txt").read_text() == "alpha, changed\n"  # asked about one file: one file


def test_unstaging_a_rename_unstages_both_its_names(ui: HeadlessUI, repo: Path) -> None:
    run_git(repo, "mv", "a.txt", "renamed.txt")
    add(ui, repo)
    settled(ui, staged=[("renamed.txt", "renamed")])
    ui.call("git.unstage", "renamed.txt")
    settled(ui, staged=[], changes=[("a.txt", "deleted")], untracked=[("renamed.txt", "untracked")])
    assert run_git(repo, "diff", "--cached", "--name-only") == ""  # nothing left to commit


def test_line_endings_alone_are_not_a_difference(ui: HeadlessUI, repo: Path) -> None:
    run_git(repo, "config", "core.autocrlf", "false")
    (repo / "dos.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    run_git(repo, "add", "dos.txt")
    run_git(repo, "commit", "-q", "-m", "a file with CRLF line endings")
    (repo / "dos.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    add(ui, repo)
    ui.call("git.diff", "dos.txt")
    assert marks(ui, "old") == [(2, "diff-changed")] and marks(ui, "new") == [(2, "diff-changed")]
    assert value(ui, "_f.diff.old_buffer.text") == "one\ntwo\nthree\n"


def test_an_open_diff_does_not_go_on_comparing(ui: HeadlessUI, repo: Path) -> None:
    (repo / "b.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    add(ui, repo)
    ui.call("git.diff", "b.txt")
    ui.eval(
        "_n = [0]\n_d = _f.diff\n_real = _d.compute\n"
        "def _counting():\n    _n[0] += 1\n    _real()\n_d.compute = _counting"
    )
    time.sleep(0.8)  # longer than it waits after an edit
    assert value(ui, "_n[0]") == 0  # its own marks are not an edit
    ui.type("x")
    wait_for(lambda: value(ui, "_n[0]") == 1, "comparing after an edit")
    time.sleep(0.8)
    assert value(ui, "_n[0]") == 1


def test_a_diff_is_not_put_away_with_its_project(
    ui: HeadlessUI, repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "o.txt").write_text("o\n")
    (repo / "b.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    add(ui, repo)
    ui.call("git.diff", "b.txt")
    assert len(windows(ui)) == 2
    add(ui, other)  # which shows it
    assert value(ui, "(_f.diff, ed.buffers.get('*b.txt (staged)*'))") == (None, None)
    ui.call("project.switch", "repo")
    assert windows(ui) == ["b.txt"]  # no window left over from the diff


def test_a_frame_that_goes_takes_its_diff_and_leaves_anothers_alone(
    ui: HeadlessUI, repo: Path
) -> None:
    (repo / "b.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    add(ui, repo)
    ui.call("git.diff", "b.txt")
    second = value(ui, "ed.buffers.get('b.txt')._listeners and 0")  # the buffer is shared
    assert second == 0
    ui.eval(
        "from neomacs import diff as _dm\n_b = ed.buffers.get('b.txt')\n"
        "class _Conn:\n    closed = False\n    def notify(self, *a): pass\n"
        "from neomacs.window import Frame as _Frame\n"
        "_g = _Frame(ed.frames[next(iter(ed.frames))].editor, _Conn(), ['panels', 'workbench'])\n"
        "_g.window.show(_b)\n_left = _g.split(_g.window, 'h', focus=False)\n"
        "_old = ed.buffers.new('*theirs*', 'one\\ntwo\\nthree\\nfour\\nfive\\n')\n_left.show(_old)\n"
        "_g.diff = _dm.Diff(_g, _left, _g.window, title='t', temporary=[_old])\n_gd = _g.diff"
    )
    # Two frames comparing one file: each has marks of its own on it.
    assert value(ui, "_gd.layer != _f.diff.layer") is True
    assert value(ui, "[len(list(_b.layer(d.layer))) for d in (_f.diff, _gd)]") == [1, 1]
    ui.eval("_g.delete()")
    assert value(ui, "(_gd.ended, _gd._changed in _b._listeners)") == (True, False)
    assert value(ui, "ed.buffers.get('*theirs*')") is None
    assert marks(ui, "new") == [(2, "diff-changed")]  # the first frame's are untouched
    assert value(ui, "len(list(_b.layer(_gd.layer)))") == 0


def test_a_command_about_the_buffers_file_is_about_its_repository(
    ui: HeadlessUI, repo: Path, tmp_path: Path
) -> None:
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    (elsewhere / "e.txt").write_text("e\n")
    run_git(elsewhere, "init", "-q", "-b", "main")
    run_git(elsewhere, "add", ".")
    run_git(elsewhere, "commit", "-q", "-m", "first")
    (elsewhere / "e.txt").write_text("e, changed\n")
    add(ui, repo)
    # A file of another repository, opened while working in this project.
    ui.eval(f"_f.window.show(ed.buffers.visit({str(elsewhere / 'e.txt')!r}))")
    assert value(ui, "(_f.workspace.name, ed.buffer.name)") == ("repo", "e.txt")
    ui.call("git.stage")
    assert ui.message == "Staged e.txt"
    assert run_git(elsewhere, "diff", "--cached", "--name-only").split() == ["e.txt"]
    ui.call("git.diff", "", True)
    assert windows(ui) == ["*e.txt (last commit)*", "*e.txt (staged)*"]


def test_the_frontend_is_told_where_head_is(ui: HeadlessUI, repo: Path) -> None:
    add(ui, repo)
    settled(ui, staged=[])
    assert ui.model.git is not None
    before = ui.model.git["head"]
    assert before == run_git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("alpha, changed\n")
    ui.call("git.stage", "a.txt")
    settled(ui, staged=[("a.txt", "modified")])
    assert ui.model.git["head"] == before  # staging is not committing
    ui.call("git.commit", "second")
    wait_for(lambda: (ui.idle(), (ui.model.git or {}).get("head") != before)[1], "HEAD moving")
