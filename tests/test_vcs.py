"""The git branch a file is on, and its place in the modeline."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from neomacs import vcs
from neomacs.testing.headless import HeadlessUI


@pytest.fixture(autouse=True)
def fresh() -> None:
    vcs.forget()


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=12, cols=60) as session:
        session.eval(f"ed.frames[{session.frame_id}].window.show(ed.buffers.new('scratch'))")
        yield session
        session.client.notify("ui.detach")


def repo(root: Path, head: str = "ref: refs/heads/main\n") -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(head)
    return root


def test_branch_of_a_file_in_a_repository(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    (root / "src" / "deep").mkdir(parents=True)
    assert vcs.branch(root / "README") == "main"
    assert vcs.branch(root / "src" / "deep" / "not-written-yet.py") == "main"
    assert vcs.branch(None) is None
    assert vcs.branch(tmp_path / "outside.txt") is None


def test_branch_names_with_slashes_and_a_detached_head(tmp_path: Path) -> None:
    root = repo(tmp_path, "ref: refs/heads/feature/find-file\n")
    assert vcs.branch(root / "a") == "feature/find-file"
    (root / ".git" / "HEAD").write_text("0df3083a1b2c3d4e5f60718293a4b5c6d7e8f901\n")
    assert vcs.branch(root / "a") == "0df3083"


def test_a_change_of_branch_is_seen(tmp_path: Path) -> None:
    root = repo(tmp_path)
    assert vcs.branch(root / "a") == "main"
    (root / ".git" / "HEAD").write_text("ref: refs/heads/another\n")
    assert vcs.branch(root / "a") == "another"


def test_a_linked_worktree(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    admin = root / ".git" / "worktrees" / "tree"
    admin.mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/side\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {admin}\n")
    assert vcs.branch(tree / "a") == "side"
    assert vcs.branch(root / "a") == "main"


def test_a_head_that_is_not_a_file_is_not_waited_on(tmp_path: Path) -> None:
    # Reading a named pipe that nothing writes to would never return.
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    os.mkfifo(root / ".git" / "HEAD")
    answers: list[str | None] = []
    asking = threading.Thread(target=lambda: answers.append(vcs.branch(root / "a")), daemon=True)
    asking.start()
    asking.join(5)
    assert answers == [None]
    # Nor a `.git` that is a pipe, where a worktree has a file.
    tree = tmp_path / "tree"
    tree.mkdir()
    os.mkfifo(tree / ".git")
    assert vcs.branch(tree / "a") is None


def test_only_the_start_of_a_huge_head_is_read(tmp_path: Path) -> None:
    root = repo(tmp_path, "ref: refs/heads/main\n" + "x" * 1_000_000)
    assert vcs.branch(root / "a") == "main"


def test_a_repository_made_later_nearer_the_file_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = repo(tmp_path / "outer", "ref: refs/heads/outer\n")
    inner = outer / "vendor" / "inner"
    inner.mkdir(parents=True)
    assert vcs.branch(inner / "a") == "outer"
    repo(inner, "ref: refs/heads/inner\n")
    assert vcs.branch(inner / "a") == "outer"  # not looked for again yet
    monkeypatch.setattr(vcs, "_RECHECK", 0.0)
    assert vcs.branch(inner / "a") == "inner"


def test_a_repository_made_by_git(tmp_path: Path) -> None:
    try:
        subprocess.run(
            ["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True, capture_output=True
        )
    except OSError, subprocess.CalledProcessError:
        pytest.skip("no git that can make a repository here")
    assert vcs.branch(tmp_path / "a") == "trunk"


def test_the_modeline_shows_the_branch(ui: HeadlessUI, tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    (root / "notes.txt").write_text("in a repository\n")
    (tmp_path / "loose.txt").write_text("not in one\n")
    assert ui.modeline_right == ""  # a buffer with no file
    ui.call("files.find", str(root / "notes.txt"))
    assert ui.modeline == " - notes.txt   L1 C0   (text) "
    assert ui.modeline_right == " \u2387 main "
    assert ui.grid()[-1] == " - notes.txt   L1 C0   (text) ".ljust(60 - 8) + " \u2387 main"
    (root / ".git" / "HEAD").write_text("ref: refs/heads/another\n")
    ui.keys("<right>")
    assert ui.modeline_right == " \u2387 another "
    ui.call("files.find", str(tmp_path / "loose.txt"))
    assert ui.modeline == " - loose.txt   L1 C0   (text) " and ui.modeline_right == ""


def test_a_branch_name_in_wide_characters_stays_inside_its_window(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = repo(tmp_path / "project", "ref: refs/heads/feature/\u4e2d\u6587\n")
    (root / "wide.txt").write_text("text\n")
    ui.call("files.find", str(root / "wide.txt"))
    assert ui.modeline_right == " \u2387 feature/\u4e2d\u6587 "
    cells = ui.compose()[-1]
    assert len(cells) == 60  # one entry for each column, and none past the edge
    row = "".join(text for text, _ in cells)
    # 16 columns: the two wide characters take two each, so 14 characters.
    assert row == " - wide.txt   L1 C0   (text) ".ljust(60 - 16) + " \u2387 feature/\u4e2d\u6587 "
