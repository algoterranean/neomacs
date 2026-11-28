"""Standard commands: git (WORKSPACE 5.5 and 8).

Staging, unstaging, discarding and committing, and the side-by-side diff.
Each takes the path it is about, relative to the top of the repository or
in full, which is how the git panel runs them; run by name they are about
the buffer's file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from neomacs import diff as diffs
from neomacs import filetree, git, modes, trash
from neomacs.commands import command
from neomacs.editor import current, ed
from neomacs.errors import NeomacsError
from neomacs.hooks import post_command
from neomacs.text.buffer import Buffer, decode


async def _repository() -> git.Repository:
    """The repository the frame's project is in, or the buffer's file, as it is now."""
    frame = ed.frame
    root = frame.workspace.root if frame is not None else None
    if root is None and ed.buffer.file is not None:
        root = ed.buffer.file.parent
    if root is None:
        raise NeomacsError("There is no project, and the buffer has no file")
    editor = current()
    editor.files_generation += 1  # ask git now, not what it said a while ago
    found = await filetree.status(editor, root)
    if found is None or found.repository is None:
        raise NeomacsError(f"{root} is not in a git repository")
    return found.repository


def _path(repository: git.Repository, given: str) -> str:
    """The path a command is about, relative to the top of the repository."""
    if given:
        path = Path(given)
        if not path.is_absolute():
            return path.as_posix()
    elif ed.buffer.file is not None:
        path = ed.buffer.file
    else:
        raise NeomacsError("The buffer has no file")
    top = repository.top.resolve()
    full = path.parent.resolve() / path.name
    if not full.is_relative_to(top):
        raise NeomacsError(f"{path} is not in the repository at {top}")
    return full.relative_to(top).as_posix()


async def _git(repository: git.Repository, *args: str, input: str | None = None) -> str:  # noqa: A002
    """Run git at the top of the repository; what it says when it fails is the error."""
    try:
        done = await git.git(repository.top, *args, input=input)
    except Exception as error:
        raise NeomacsError(f"git could not be run: {error}") from None
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip()).splitlines()
        raise NeomacsError(f"git: {said[-1] if said else 'failed'}")
    return done.stdout


def _changed() -> None:
    filetree.changed(current())


def _reload(buffer: Buffer) -> None:
    """Make BUFFER's text what its file now holds."""
    file = buffer.file
    if file is None or not file.is_file():
        return
    text = decode(file.read_bytes())[0]
    if text != buffer.text:
        read_only, buffer.read_only = buffer.read_only, False
        try:
            buffer.replace(0, len(buffer), text)
        finally:
            buffer.read_only = read_only
    buffer.saved_tick = buffer.tick


@command(name="git.stage")
async def stage(path: str = "") -> None:
    """Stage a file's changes for the next commit."""
    repository = await _repository()
    target = _path(repository, path)
    await _git(repository, "add", "--", target)
    _changed()
    ed.message(f"Staged {target}")


@command(name="git.unstage")
async def unstage(path: str = "") -> None:
    """Take a file's changes out of the next commit.  The file is not touched."""
    repository = await _repository()
    target = _path(repository, path)
    await _git(repository, "reset", "-q", "--", target)
    _changed()
    ed.message(f"Unstaged {target}")


@command(name="git.stage_all")
async def stage_all(which: str = "") -> None:
    """Stage every change in the repository, new files included.  WHICH may
    be "changes", for the files git knows only, or "untracked", for the new
    ones only."""
    repository = await _repository()
    if which == "changes":
        await _git(repository, "add", "-u")
    elif which == "untracked":
        if repository.untracked:
            await _git(repository, "add", "--", *repository.untracked)
    elif which == "":
        await _git(repository, "add", "-A")
    else:
        raise NeomacsError(f"Not a kind of change: {which}")
    _changed()
    said = {"changes": "Staged every change", "untracked": "Staged every new file"}
    ed.message(said.get(which, "Staged everything"))


@command(name="git.unstage_all")
async def unstage_all() -> None:
    """Take everything out of the next commit.  No file is touched."""
    repository = await _repository()
    await _git(repository, "reset", "-q")
    _changed()
    ed.message("Unstaged everything")


@command(name="git.discard")
async def discard(path: str = "") -> None:
    """Throw away a file's changes that are not staged, after asking.  A file
    git has never seen is moved to the trash."""
    repository = await _repository()
    target = _path(repository, path)
    full = repository.top / target
    untracked = target in repository.untracked
    if not untracked and target not in [name for name, _ in repository.changes]:
        raise NeomacsError(f"{target} has no changes to discard")
    has_trash = trash.trash_dir() is not None
    if untracked:
        question = f"Move {target} to the trash?" if has_trash else f"Delete {target} for good?"
    else:
        question = f"Discard the changes to {target}? They cannot be got back."
    answer = await ed.minibuffer.read(f"{question} (y or n) ")
    if answer.strip().lower() not in ("y", "yes"):
        ed.message("Nothing was discarded")
        return
    if untracked:
        try:
            if has_trash:
                trash.move_to_trash(full)
            else:
                trash.delete_for_good(full)
        except OSError as error:
            raise NeomacsError(f"Could not delete {target}: {error}") from None
    else:
        await _git(repository, "checkout", "--", target)
        for buffer in ed.buffers:
            if buffer.file == full:
                _reload(buffer)  # unsaved changes to it are changes too, and go
    _changed()
    ed.message(f"Discarded the changes to {target}" if not untracked else f"Removed {target}")


@command(name="git.commit")
async def commit(message: str = "") -> None:
    """Commit what is staged.  The message is asked for unless one is given."""
    repository = await _repository()
    if not repository.staged:
        raise NeomacsError("Nothing is staged")
    if not message:
        message = await ed.minibuffer.read(
            "Commit message (C-Return commits): ", multiline=True, history="commits"
        )
    if not message.strip():
        raise NeomacsError("A commit needs a message")
    await _git(repository, "commit", "-q", "-F", "-", input=message.strip() + "\n")
    _changed()
    ed.message(f"Committed: {message.strip().splitlines()[0]}")


@command(name="git.refresh")
def refresh() -> None:
    """Ask git again what has changed."""
    _changed()


# -- the diff (WORKSPACE 8) -----------------------------------------------------------


def _temporary(name: str, text: str, like: str) -> Buffer:
    """A read-only buffer holding TEXT, a version of the file called LIKE."""
    buffer = ed.buffers.new(name, text)
    buffer.set_mode(modes.mode_for(like, text.split("\n", 1)[0]))
    buffer.undo.clear()
    buffer.saved_tick = buffer.tick
    buffer.read_only = True
    return buffer


@command(name="git.diff")
async def diff(path: str = "", staged: bool = False) -> None:
    """Show what has changed in a file, side by side: at the left what git
    has, at the right the file.  With STAGED, the last commit at the left and
    what is staged at the right."""
    repository = await _repository()
    target = _path(repository, path)
    full = repository.top / target
    frame = ed.frame
    assert frame is not None
    if target in repository.untracked:
        ed.show(ed.buffers.visit(full), focus=True)
        ed.message(f"{target} is new: there is nothing to compare it with")
        return
    name = Path(target).name
    if frame.diff is not None:
        frame.diff.end(close=True)  # one at a time: the last one's buffers and window go
    made: list[Buffer] = []
    if staged:
        before = await git.show(repository.top, f"HEAD:{target}")
        after = await git.show(repository.top, f":{target}")
        if after is None:
            raise NeomacsError(f"{target} is not staged")
        old = _temporary(f"*{name} (last commit)*", before or "", name)
        new = _temporary(f"*{name} (staged)*", after, name)
        made = [old, new]
        title = f"{target}: last commit ↔ staged"
    else:
        before = await git.show(repository.top, f":{target}")
        if before is None:
            raise NeomacsError(f"git has no version of {target} to compare with")
        if not full.is_file():
            raise NeomacsError(f"{target} has been deleted: there is no file to show")
        old = _temporary(f"*{name} (staged)*", before, name)
        made = [old]
        new = ed.buffers.visit(full)
        title = f"{target}: staged ↔ file"
    # The file's side goes in the window as any buffer would, so that window
    # keeps its tabs; the old side gets a window of its own at its left.
    ed.show(new, focus=True)
    right = frame.window
    left = frame.split(right, "h", focus=False)
    parent: Any = frame._parent_of(left)  # pyright: ignore[reportPrivateUsage]
    if parent is not None:
        a, b = parent.children.index(right), parent.children.index(left)
        parent.children[a], parent.children[b] = left, right
    left.show(old)
    left.tabs[:] = [old]
    shown = diffs.Diff(frame, left, right, title=title, temporary=made)
    frame.diff = shown
    changes = shown.changes()
    if shown.too_big:
        ed.message(f"{title}. Too long to mark what differs.")
    elif not changes:
        ed.message(f"{title}. The two are the same.")
    else:
        _go(shown, right, changes[0])
        count = len(changes)
        ed.message(f"{title}. {count} change{'s' if count != 1 else ''}.")


def _go(shown: diffs.Diff, view: Any, block: diffs.Block) -> None:
    """Put VIEW's cursor at BLOCK, and bring the other side to match."""
    side = shown.side(view)
    line = (block[1] if side == 0 else block[3]) + 1
    line = max(1, min(view.buffer.line_count, line))
    start = view.buffer.line_start(line)
    view.selections.set([(start, start)])
    view.reveal(start, "center")
    other = shown.new if side == 0 else shown.old
    there = max(1, min(other.buffer.line_count, (block[3] if side == 0 else block[1]) + 1))
    start = other.buffer.line_start(there)
    other.selections.set([(start, start)])
    other.reveal(start, "center")


def _step(direction: int) -> None:
    frame = ed.frame
    shown: diffs.Diff | None = frame.diff if frame is not None else None
    if shown is None or not shown.alive():
        raise NeomacsError("No diff is on show")
    assert frame is not None
    view: Any = frame.focus if shown.side(frame.focus) is not None else shown.new
    side = shown.side(view)
    changes = shown.changes()
    if not changes:
        raise NeomacsError("The two are the same")
    here = view.buffer.line_offset(view.cursor.pos)[0] - 1
    starts = [block[1] if side == 0 else block[3] for block in changes]
    if direction > 0:
        index = next((i for i, start in enumerate(starts) if start > here), None)
    else:
        index = next((i for i in reversed(range(len(starts))) if starts[i] < here), None)
    if index is None:
        raise NeomacsError("No more changes that way")
    _go(shown, view, changes[index])
    ed.message(f"Change {index + 1} of {len(changes)}")


@command(name="git.next_change")
def next_change() -> None:
    """In a diff, go to the next place the two sides differ."""
    _step(1)


@command(name="git.previous_change")
def previous_change() -> None:
    """In a diff, go to the previous place the two sides differ."""
    _step(-1)


@command(name="git.close_diff")
def close_diff() -> None:
    """End the diff on show: its marks go, and the window with the old version."""
    frame = ed.frame
    if frame is None or frame.diff is None:
        raise NeomacsError("No diff is on show")
    frame.diff.end(close=True)


@post_command.add
def _after_command(_command: Any) -> None:
    # Closing either window, or showing something else in one, ends the diff.
    for frame in list(ed.frames.values()):
        if frame.diff is not None:
            diffs.check(frame)
