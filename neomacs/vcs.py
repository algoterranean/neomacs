"""What version control says about a file.  For now: the git branch it is on.

The branch is read from the repository's HEAD file rather than by running git,
so asking costs one stat when nothing has changed.  The modeline asks on every
view update.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from neomacs.safeio import read_regular

# Shown before a branch name.  Unicode has no character for a branch; this one,
# the alternative key symbol, is what is generally used for it.
BRANCH_SYMBOL = "\u2387"

# How long what was found for a directory, a repository or none, is taken to
# still be so.  A repository made later, around the file or nearer to it than
# the one found, is noticed after this long.
_RECHECK = 5.0
# As much of a file as is read.  Both files read here are a line long.
_READ_LIMIT = 4096

# directory -> (the HEAD file of the repository it is in, when that was looked up)
_heads: dict[Path, tuple[Path | None, float]] = {}
# HEAD file -> (what stat said about it, the branch read from it)
_branches: dict[Path, tuple[tuple[int, int, int], str | None]] = {}


def _read_start(path: Path) -> str:
    """The start of the regular file at PATH; OSError if it is anything else."""
    return read_regular(path, _READ_LIMIT).decode("utf-8", errors="replace")


def _find_head(directory: Path) -> Path | None:
    """The HEAD file of the repository DIRECTORY is in, if it is in one."""
    for parent in (directory, *directory.parents):
        dot_git = parent / ".git"
        try:
            if dot_git.is_dir():
                return dot_git / "HEAD"
            if dot_git.is_file():  # a linked worktree or a submodule
                first = _read_start(dot_git).split("\n", 1)[0]
                if first.startswith("gitdir:"):
                    return parent / first[len("gitdir:") :].strip() / "HEAD"
        except OSError:
            return None
    return None


def _parse_head(text: str) -> str | None:
    head = text.split("\n", 1)[0].strip()
    if head.startswith("ref:"):
        ref = head[len("ref:") :].strip()
        name = ref.removeprefix("refs/heads/")
        # A repository that keeps its refs in a reftable has a placeholder here.
        return None if name == ".invalid" else name
    return head[:7] or None  # detached: the start of the commit id


def branch(file: Path | None) -> str | None:
    """The git branch FILE is on, the start of the commit id if HEAD is
    detached, or None if the file is not in a repository."""
    if file is None:
        return None
    directory = file.parent
    now = time.monotonic()
    found = _heads.get(directory)
    if found is None or now - found[1] > _RECHECK:
        found = _heads[directory] = (_find_head(directory), now)
    head = found[0]
    if head is None:
        return None
    try:
        st = os.stat(head)
    except OSError:
        _heads.pop(directory, None)  # the repository went away: look again next time
        _branches.pop(head, None)
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _branches.get(head)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        name = _parse_head(_read_start(head))
    except OSError:
        name = None
    _branches[head] = (key, name)
    return name


def forget() -> None:
    """Drop everything remembered, so the next question looks at the disk."""
    _heads.clear()
    _branches.clear()
