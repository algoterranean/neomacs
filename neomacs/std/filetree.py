"""Standard commands: making, renaming and deleting files (WORKSPACE 5.4).

Each takes the path it is about, which is how the file tree's menu runs them;
run by name or by a key they are about the buffer's file, or the directory
it is in.
"""

from __future__ import annotations

from pathlib import Path

from neomacs import filetree, modes, trash
from neomacs.commands import command
from neomacs.editor import current, ed
from neomacs.errors import NeomacsError
from neomacs.text.buffer import Buffer


def _directory(given: str) -> Path:
    """The directory a new file goes in: GIVEN, or the one the buffer's file
    is in, or the project's root."""
    if given:
        path = Path(given).expanduser()
        if not path.is_dir():
            raise NeomacsError(f"{path} is not a directory")
        return path.resolve()
    file = ed.buffer.file
    if file is not None and file.parent.is_dir():
        return file.parent
    frame = ed.frame
    root = frame.workspace.root if frame is not None else None
    if root is None:
        raise NeomacsError("There is no project, and the buffer has no file")
    return root


def _file(given: str) -> Path:
    """The file or directory a command is about: GIVEN, or the buffer's file."""
    if given:
        path = Path(given).expanduser()
    elif ed.buffer.file is not None:
        path = ed.buffer.file
    else:
        raise NeomacsError("The buffer has no file")
    if not (path.exists() or path.is_symlink()):
        raise NeomacsError(f"{path} does not exist")
    return path.parent.resolve() / path.name  # the link itself, not what it points to


def _shown(path: Path) -> str:
    """PATH as the user is told of it: from the project's root if it is under it."""
    frame = ed.frame
    root = frame.workspace.root if frame is not None else None
    if root is not None and path.is_relative_to(root) and path != root:
        return path.relative_to(root).as_posix()
    return str(path)


def _named(directory: Path, name: str) -> Path:
    name = name.strip()
    if not name:
        raise NeomacsError("No name was given")
    target = (directory / Path(name).expanduser()).resolve()
    if target.exists() or target.is_symlink():
        raise NeomacsError(f"{_shown(target)} exists already")
    return target


def _visiting(path: Path) -> list[Buffer]:
    """The buffers whose file is PATH or is under it."""
    return [
        buffer
        for buffer in ed.buffers
        if buffer.file is not None and (buffer.file == path or path in buffer.file.parents)
    ]


@command(name="files.new_file")
async def new_file(directory: str = "") -> None:
    """Make an empty file and visit it.  The name is asked for; it may have
    directories in it, which are made too."""
    where = _directory(directory)
    name = await ed.minibuffer.read(f"New file in {_shown(where)}/: ", history="files")
    target = _named(where, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    filetree.changed(current())
    ed.show(ed.buffers.visit(target), focus=True)


@command(name="files.new_folder")
async def new_folder(directory: str = "") -> None:
    """Make a directory.  The name is asked for."""
    where = _directory(directory)
    name = await ed.minibuffer.read(f"New folder in {_shown(where)}/: ", history="files")
    target = _named(where, name)
    target.mkdir(parents=True)
    filetree.changed(current())
    ed.message(f"Made {_shown(target)}/")


@command(name="files.rename")
async def rename(path: str = "") -> None:
    """Give a file or directory another name, asked for.  Buffers visiting it,
    or files under it, go on visiting them where they are now."""
    old = _file(path)
    name = await ed.minibuffer.read(f"Rename {_shown(old)} to: ", initial=old.name, history="files")
    if name.strip() == old.name:
        return
    new = _named(old.parent, name)
    visiting = _visiting(old)
    # A buffer may be visiting the new name though no file has it yet: two
    # buffers on one file would each overwrite the other's work.
    taken = [b for b in _visiting(new) if b not in visiting]
    if taken:
        raise NeomacsError(f"The buffer {taken[0].name} is visiting {_shown(new)} already")
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    for buffer in visiting:
        assert buffer.file is not None
        was = buffer.file
        buffer.file = new if was == old else new / was.relative_to(old)
        if buffer.file.name != was.name:
            ed.buffers.rename(buffer, buffer.file.name)
            if buffer.file.suffix != was.suffix:
                buffer.set_mode(modes.mode_for(buffer.file.name, buffer.line_text(1)))
        # It may be in another project now, as after a save under another name.
        ed.workspaces.rehome(buffer)
    filetree.changed(current())
    ed.message(f"Renamed {_shown(old)} to {_shown(new)}")


@command(name="files.delete")
async def delete(path: str = "") -> None:
    """Move a file or directory to the trash, after asking.  Where the system
    has no trash it is deleted for good, and the question says so.  Buffers
    visiting it are killed, but for any with unsaved changes."""
    target = _file(path)
    has_trash = trash.trash_dir() is not None
    what = f"{_shown(target)}/ and everything in it" if target.is_dir() else _shown(target)
    question = f"Move {what} to the trash?" if has_trash else f"Delete {what} for good?"
    answer = await ed.minibuffer.read(f"{question} (y or n) ")
    if answer.strip().lower() not in ("y", "yes"):
        ed.message("Nothing was deleted")
        return
    visiting = _visiting(target)
    try:
        if has_trash:
            trash.move_to_trash(target)
        else:
            trash.delete_for_good(target)
    except OSError as error:
        raise NeomacsError(f"Could not delete {_shown(target)}: {error}") from None
    kept = 0
    for buffer in visiting:
        if buffer.modified:
            kept += 1  # the text is all that is left of it: saving writes the file again
            continue
        for frame in list(ed.frames.values()):
            for view in list(frame.views.values()):
                panel = getattr(view, "panel", None)
                if panel is None:
                    view.forget(buffer, ed.workspaces._instead(frame.workspace, buffer))  # pyright: ignore[reportPrivateUsage]
                elif view.buffer is buffer:
                    panel.close(view)
        ed.buffers.kill(buffer)
    filetree.changed(current())
    done = "Moved to the trash" if has_trash else "Deleted"
    note = f"; {kept} unsaved buffer{'s' if kept != 1 else ''} kept" if kept else ""
    ed.message(f"{done}: {_shown(target)}{note}")


@command(name="files.copy_path")
def copy_path(path: str = "") -> None:
    """Copy a file's full path: of the buffer's file, unless one is given."""
    target = _file(path)
    ed.kill_ring.push([str(target)], ed.frame, source="copy")
    ed.message(f"Copied {target}")


@command(name="files.refresh")
def refresh() -> None:
    """Read the file tree again from the disk."""
    filetree.changed(current())


@command(name="view.focus_files")
def focus_files() -> None:
    """Move the keyboard to the file tree.  Escape gives it back to the text."""
    frame = ed.frame
    if frame is None or not frame.draws_workbench or frame.conn is None:
        raise NeomacsError("This frontend has no file tree")
    from neomacs import workbench

    workbench.set_region(frame, "details", True)
    frame.conn.notify("ui.focus", "files")
