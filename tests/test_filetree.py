"""The file tree (WORKSPACE.md, stage W-4): directory listings with git
state, and the commands that make, rename and delete files."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs import git
from neomacs.testing import running_engine
from neomacs.testing.client import RemoteError
from neomacs.testing.headless import HeadlessUI

WORKBENCH = {"capabilities": ["panels", "workbench"]}


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A trash of the test's own: nothing here goes near the real one."""
    where = tmp_path / "the-trash"
    monkeypatch.setenv("NEOMACS_TRASH_DIR", str(where))
    return where


@pytest.fixture
def ui(trash: Path) -> Iterator[HeadlessUI]:
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70, attach=WORKBENCH) as session:
        session.eval(f"_f = ed.frames[{session.frame_id}]")
        yield session


def run_git(root: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }  # fmt: skip
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for name in ("b.txt", "A.txt", "src/main.py", "src/util/deep.py", "docs/guide.md", "old.txt"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"{name}\n")
    return root.resolve()


def add(ui: HeadlessUI, root: Path) -> int:
    ui.call("project.add", str(root))
    return value(ui, "_f.workspace.id")


def listing(ui: HeadlessUI, workspace: int, directory: str = "") -> list[dict[str, Any]]:
    return ui.client.call("files.list", workspace, directory)


def names(entries: list[dict[str, Any]]) -> list[str]:
    return [e["name"] + ("/" if e["kind"] == "directory" else "") for e in entries]


def states(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {e["name"]: ("ignored" if e["ignored"] else e["git"]) for e in entries}


# -- listings ------------------------------------------------------------------------


def test_a_directory_is_listed_directories_first_then_by_name(
    ui: HeadlessUI, project: Path
) -> None:
    workspace = add(ui, project)
    assert names(listing(ui, workspace)) == ["docs/", "src/", "A.txt", "b.txt", "old.txt"]
    assert names(listing(ui, workspace, "src")) == ["util/", "main.py"]
    assert names(listing(ui, workspace, "src/util")) == ["deep.py"]
    # Not in a repository: nothing has a state.
    assert set(states(listing(ui, workspace)).values()) == {None}


def test_only_the_projects_own_directories_can_be_listed(ui: HeadlessUI, project: Path) -> None:
    workspace = add(ui, project)
    for directory in ("..", "src/../..", "/etc", "src\\util", "nowhere"):
        with pytest.raises(RemoteError):
            listing(ui, workspace, directory)
    with pytest.raises(RemoteError, match="no such project"):
        listing(ui, 9999)
    none = value(ui, "ed.workspaces.none.id")
    with pytest.raises(RemoteError, match="no such project"):
        listing(ui, none)  # it has no directory


def test_entries_carry_their_git_state(ui: HeadlessUI, project: Path) -> None:
    (project / ".gitignore").write_text("*.log\nbuild/\n")
    run_git(project, "init", "-q")
    run_git(project, "add", ".")
    run_git(project, "commit", "-q", "-m", "first")
    (project / "b.txt").write_text("changed\n")  # modified
    (project / "A.txt").write_text("staged\n")
    run_git(project, "add", "A.txt")  # staged
    (project / "new.txt").write_text("new\n")  # untracked
    (project / "fresh" / "inner").mkdir(parents=True)
    (project / "fresh" / "inner" / "x.txt").write_text("x\n")  # a directory that is all new
    (project / "debug.log").write_text("log\n")  # ignored
    (project / "build").mkdir()
    (project / "build" / "out.o").write_text("o\n")
    (project / "src" / "util" / "deep.py").write_text("changed\n")  # deep inside src
    workspace = add(ui, project)
    top = listing(ui, workspace)
    assert ".git" not in names(top) and ".git/" not in names(top)
    assert states(top) == {
        "build": "ignored", "docs": None, "fresh": "untracked", "src": "modified",
        ".gitignore": None, "A.txt": "staged", "b.txt": "modified", "debug.log": "ignored",
        "new.txt": "untracked", "old.txt": None,
    }  # fmt: skip
    assert states(listing(ui, workspace, "src")) == {"util": "modified", "main.py": None}
    assert states(listing(ui, workspace, "fresh")) == {"inner": "untracked"}
    assert states(listing(ui, workspace, "fresh/inner")) == {"x.txt": "untracked"}
    assert states(listing(ui, workspace, "build")) == {"out.o": "ignored"}


def test_a_project_below_the_top_of_its_repository(ui: HeadlessUI, project: Path) -> None:
    run_git(project, "init", "-q")
    run_git(project, "add", ".")
    run_git(project, "commit", "-q", "-m", "first")
    (project / "src" / "main.py").write_text("changed\n")
    (project / "b.txt").write_text("changed, but not in the project\n")
    workspace = add(ui, project / "src")
    assert states(listing(ui, workspace)) == {"util": None, "main.py": "modified"}


def test_git_status_is_read_as_git_writes_it() -> None:
    output = "\0".join(
        [
            "# branch.head main",
            "1 .M N... 100644 100644 100644 aaa bbb with space.txt",
            "1 M. N... 100644 100644 100644 aaa bbb staged.txt",
            "1 MM N... 100644 100644 100644 aaa bbb both.txt",
            "2 R. N... 100644 100644 100644 aaa bbb R100 new name.txt",
            "old name.txt",
            "u UU N... 100644 100644 100644 100644 aaa bbb ccc fight.txt",
            "? fresh/",
            "! build/",
            "! notes.log",
            "",
        ]
    )
    status = git.parse(output)
    assert status.states == {
        "with space.txt": "modified", "staged.txt": "staged", "both.txt": "modified",
        "new name.txt": "staged", "fight.txt": "conflicted", "fresh/": "untracked",
    }  # fmt: skip
    assert status.ignored == {"build/", "notes.log"}
    assert status.of("fresh/a/b.txt") == "untracked" and status.of("fresh", True) == "untracked"
    assert status.is_ignored("build/x/y.o") and status.is_ignored("build")
    assert not status.is_ignored("builder.txt") and status.of("staged.txt.bak") is None
    # Under a prefix: the rest is left out, and the paths are from there.
    inner = git.parse("1 .M N... 1 1 1 a b sub/x.txt\0? other.txt\0", "sub/")
    assert inner.states == {"x.txt": "modified"}
    # The most pressing state inside is the directory's.
    mixed = git.parse("? d/a\x001 M. N... 1 1 1 a b d/b\x00u UU N... 1 1 1 1 a b c d/c\x00")
    assert mixed.of("d", True) == "conflicted"


# -- being told to ask again -----------------------------------------------------------


def stale(ui: HeadlessUI) -> int:
    """How many times this frontend has been told its listings are stale."""
    return ui.model.files_changed


def test_the_frontend_is_told_when_its_listings_may_be_stale(ui: HeadlessUI, project: Path) -> None:
    add(ui, project)
    ui.open(project / "b.txt")
    before = stale(ui)
    ui.type("x")
    assert stale(ui) == before  # typing changes no file
    ui.keys("C-x", "C-s")
    assert stale(ui) == before + 1
    ui.call("files.refresh")
    assert stale(ui) == before + 2
    ui.client.notify("input.focus", True)  # the user has been elsewhere
    ui.idle()
    assert stale(ui) == before + 3


def test_a_frontend_that_draws_no_tree_is_told_nothing(trash: Path) -> None:
    with running_engine() as path, HeadlessUI(path, rows=14, cols=70) as plain:
        plain.call("files.refresh")
        plain.client.notify("input.focus", True)
        plain.idle()
        assert plain.model.files_changed == 0


# -- the commands ----------------------------------------------------------------------


def answer(ui: HeadlessUI, command: str, args: list[Any], text: str) -> str:
    """Run a command that asks, type TEXT at its question, and return the question."""
    ui.client.notify("engine.call", command, args)
    ui.idle()
    asked = ui.prompt
    assert asked is not None, f"{command} asked nothing"
    ui.keys("C-a", "C-k")
    ui.type(text)
    ui.keys("<return>")
    return asked


def test_new_files_and_folders(ui: HeadlessUI, project: Path) -> None:
    workspace = add(ui, project)
    before = stale(ui)
    asked = answer(ui, "files.new_file", [str(project / "src")], "fresh.py")
    assert asked == "New file in src/: "
    assert (project / "src" / "fresh.py").read_text() == ""
    assert value(ui, "(ed.buffer.name, _f.focus is _f.window)") == ("fresh.py", True)
    assert stale(ui) > before and "fresh.py" in names(listing(ui, workspace, "src"))
    # With no directory given: beside the buffer's file.  Directories in the name are made.
    answer(ui, "files.new_file", [], "pkg/mod.py")
    assert (project / "src" / "pkg" / "mod.py").is_file()
    asked = answer(ui, "files.new_folder", [str(project)], "assets")
    assert asked == "New folder in proj/: " or asked.startswith("New folder in ")
    assert (project / "assets").is_dir() and ui.message == "Made assets/"
    answer(ui, "files.new_file", [str(project)], "b.txt")
    assert ui.message == "b.txt exists already" and (project / "b.txt").read_text() == "b.txt\n"


def test_renaming_takes_the_buffers_along(ui: HeadlessUI, project: Path) -> None:
    add(ui, project)
    ui.open(project / "src" / "main.py")
    ui.open(project / "b.txt")
    ui.type("unsaved ")
    asked = answer(ui, "files.rename", [], "c.md")  # the buffer's own file
    assert asked == "Rename b.txt to: b.txt"
    assert not (project / "b.txt").exists() and (project / "c.md").read_text() == "b.txt\n"
    assert value(ui, "(ed.buffer.name, ed.buffer.file.name, ed.buffer.modified)") == (
        "c.md", "c.md", True,
    )  # fmt: skip
    assert value(ui, "ed.buffers.get('b.txt')") is None
    assert value(ui, "[b.name for b in _f.window.tabs]") == ["main.py", "c.md"]
    ui.keys("C-x", "C-s")
    assert (project / "c.md").read_text() == "unsaved b.txt\n"
    # A directory: the buffers of files under it follow.
    answer(ui, "files.rename", [str(project / "src")], "lib")
    assert (project / "lib" / "main.py").is_file() and not (project / "src").exists()
    assert value(ui, "str(ed.buffers.get('main.py').file)") == str(project / "lib" / "main.py")
    assert ui.message == "Renamed src to lib"
    answer(ui, "files.rename", [str(project / "A.txt")], "old.txt")
    assert ui.message == "old.txt exists already" and (project / "A.txt").exists()


def test_deleting_asks_first_and_moves_to_the_trash(
    ui: HeadlessUI, project: Path, trash: Path
) -> None:
    add(ui, project)
    ui.open(project / "old.txt")
    ui.open(project / "b.txt")
    asked = answer(ui, "files.delete", [str(project / "old.txt")], "n")
    assert asked == "Move old.txt to the trash? (y or n) "
    assert (project / "old.txt").exists() and ui.message == "Nothing was deleted"
    answer(ui, "files.delete", [str(project / "old.txt")], "y")
    assert not (project / "old.txt").exists()
    assert (trash / "old.txt").read_text() == "old.txt\n"
    assert ui.message == "Moved to the trash: old.txt"
    # The buffer that was visiting it has gone, and its tab with it.
    assert value(ui, "ed.buffers.get('old.txt')") is None
    assert value(ui, "[b.name for b in _f.window.tabs]") == ["b.txt"]
    # One with unsaved changes is kept: its text is all that is left.
    ui.type("precious ")
    answer(ui, "files.delete", [], "yes")
    assert not (project / "b.txt").exists()
    assert ui.message == "Moved to the trash: b.txt; 1 unsaved buffer kept"
    assert value(ui, "ed.buffer.text") == "precious b.txt\n"
    # A directory goes whole; a second thing of the same name does not replace the first.
    asked = answer(ui, "files.delete", [str(project / "src")], "y")
    assert asked == "Move src/ and everything in it to the trash? (y or n) "
    assert (trash / "src" / "util" / "deep.py").is_file() and not (project / "src").exists()
    (project / "old.txt").write_text("again\n")
    answer(ui, "files.delete", [str(project / "old.txt")], "y")
    assert (trash / "old.txt").read_text() == "old.txt\n"
    assert (trash / "old 2.txt").read_text() == "again\n"
    ui.call("files.delete", str(project / "nothing-here"))
    assert ui.message.endswith("does not exist")


def test_copying_a_path(ui: HeadlessUI, project: Path) -> None:
    add(ui, project)
    ui.call("files.copy_path", str(project / "docs" / "guide.md"))
    assert value(ui, "ed.kill_ring.entries[-1].text") == str(project / "docs" / "guide.md")
    ui.open(project / "b.txt")
    ui.call("files.copy_path")
    assert value(ui, "ed.kill_ring.entries[-1].text") == str(project / "b.txt")
