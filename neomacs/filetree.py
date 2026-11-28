"""The files of a project, as the file tree of the details sidebar asks for
them (WORKSPACE 5.4): a directory at a time, each entry with its git state.

Nothing watches the disk.  What a frontend holds is said to be stale, by
`changed`, at the moments it is likely to be: after a save, after a file
command, when the window gets the keyboard back, and when the user asks.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from neomacs import git
from neomacs.text.buffer import Buffer, after_save

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.workspace import Workspace

HIDDEN = (".git",)  # never listed
MAX_ENTRIES = 5000  # of one directory; one with more is cut short


def changed(editor: Editor) -> None:
    """Listings that frontends hold may be out of date: have them ask again."""
    editor.files_generation += 1
    editor.update_frames()


def inside(root: Path, directory: str) -> Path:
    """The directory DIRECTORY names under ROOT.  It is given relative to the
    root with `/` between its parts, "" for the root itself; ValueError if it
    is anything that could name a place outside."""
    relative = PurePosixPath(directory or ".")
    if relative.is_absolute() or ".." in relative.parts or "\\" in directory or "\0" in directory:
        raise ValueError(f"not a directory of the project: {directory!r}")
    return root.joinpath(*relative.parts)


async def _status(editor: Editor, root: Path) -> git.Status | None:
    """The git state under ROOT, asked of git once for each time the files
    changed.  A tree with many folders open asks for them all at once: they
    share the one question, answered or not yet."""
    cache: dict[Path, tuple[int, asyncio.Future[git.Status | None]]] = editor.files_status
    held = cache.get(root)
    generation = editor.files_generation
    loop = asyncio.get_running_loop()
    if held is not None and held[0] == generation and held[1].get_loop() is loop:
        asked = held[1]
        if not (asked.done() and (asked.cancelled() or asked.exception() is not None)):
            # Shielded: one asker giving up does not take the answer from the rest.
            return await asyncio.shield(asked)
    asked = loop.create_task(git.status(root))
    cache[root] = (generation, asked)
    return await asyncio.shield(asked)


async def list_directory(
    editor: Editor, workspace: Workspace, directory: str = ""
) -> list[dict[str, Any]]:
    """The entries of DIRECTORY in WORKSPACE: directories first, then files,
    each by name.  An entry is `{"name", "kind", "git", "ignored"}`: the kind
    is "directory" or "file", the git state one of `git.STATES` or None."""
    root = workspace.root
    if root is None:
        return []
    target = inside(root, directory)
    status = await _status(editor, root)
    prefix = "" if directory in ("", ".") else directory.strip("/") + "/"
    entries: list[dict[str, Any]] = []
    with os.scandir(target) as found:
        for entry in found:
            if entry.name in HIDDEN:
                continue
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            path = prefix + entry.name
            entries.append(
                {
                    "name": entry.name,
                    "kind": "directory" if is_directory else "file",
                    "git": status.of(path, is_directory) if status is not None else None,
                    "ignored": status.is_ignored(path) if status is not None else False,
                }
            )
            if len(entries) >= MAX_ENTRIES:
                break
    entries.sort(key=lambda e: (e["kind"] != "directory", e["name"].casefold(), e["name"]))
    return entries


def _saved(buffer: Buffer) -> None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    if editor is not None and hasattr(editor, "files_generation"):
        changed(editor)


after_save.add(_saved)
