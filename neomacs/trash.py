"""Moving a file to the system's trash, where there is one (WORKSPACE 5.4).

On macOS that is `~/.Trash`; elsewhere on POSIX the home trash of the
freedesktop.org specification.  Windows has no trash that can be reached
without its shell: there `trash_dir` is None, and the caller deletes for good
after saying so.  `NEOMACS_TRASH_DIR` names a directory to use instead, which
is how the tests keep out of the real one.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import quote


def trash_dir() -> Path | None:
    """Where trashed files go, or None when this system has no trash to move to."""
    override = os.environ.get("NEOMACS_TRASH_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return None
    if sys.platform == "darwin":
        return Path.home() / ".Trash"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "Trash"


def _free_name(directory: Path, name: str) -> Path:
    target = directory / name
    count = 2
    stem, suffix = Path(name).stem, Path(name).suffix
    while target.exists() or target.is_symlink():
        target = directory / f"{stem} {count}{suffix}"
        count += 1
    return target


def move_to_trash(path: Path) -> Path:
    """Move PATH to the trash and return where it is now.  OSError if there
    is no trash, or it cannot be moved there."""
    trash = trash_dir()
    if trash is None:
        raise OSError("this system has no trash to move to")
    freedesktop = sys.platform not in ("darwin", "win32") and "NEOMACS_TRASH_DIR" not in os.environ
    files = trash / "files" if freedesktop else trash
    files.mkdir(parents=True, exist_ok=True)
    target = _free_name(files, path.name)
    if freedesktop:
        # The specification's record of where it came from, written first.
        info = trash / "info"
        info.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        (info / f"{target.name}.trashinfo").write_text(
            f"[Trash Info]\nPath={quote(str(path.absolute()))}\nDeletionDate={stamp}\n"
        )
    shutil.move(str(path), str(target))
    return target


def delete_for_good(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
