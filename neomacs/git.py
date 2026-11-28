"""What git says about the files of a directory (WORKSPACE 5.4, 5.5).

Git is run for this, unlike the branch name in `neomacs.vcs`, which is read
from a file because it is asked for on every update.  Nothing here is asked
for that often: a listing is read when the file tree is.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neomacs import proc

# The states a file can be in, the most pressing first: a directory takes the
# most pressing state of anything inside it.
STATES = ("conflicted", "modified", "staged", "untracked")
_TIMEOUT = 10.0
# Looking must not write to the repository, nor wait for a prompt.
# A path given to git is that file and no other: `*.txt` is a name, not a pattern.
_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1"}


# What the letters of git's two-letter state mean.
_LETTERS = {
    "M": "modified", "T": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied",
}  # fmt: skip


@dataclass
class Repository:
    """A repository as `git status` describes it.  Paths are relative to its
    top, with `/` between their parts.  A file changed both in the index and
    since is in `staged` and in `changes`."""

    top: Path
    branch: str | None = None  # None when HEAD is detached
    head: str | None = None  # the commit HEAD is at; None before the first
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    changes: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    untracked: list[str] = field(default_factory=list[str])
    conflicted: list[str] = field(default_factory=list[str])
    # For a staged rename or copy: the name it has now -> the name it had.
    renamed: dict[str, str] = field(default_factory=dict[str, str])

    def describe(self, limit: int = 2000) -> dict[str, Any]:
        """What a frontend is told (WORKSPACE 6).  A list longer than LIMIT is cut short."""

        def rows(found: list[tuple[str, str]]) -> list[dict[str, str]]:
            return [{"path": path, "state": state} for path, state in found[:limit]]

        return {
            "top": str(self.top),
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "staged": rows(self.staged),
            "changes": rows(self.changes),
            "untracked": rows([(path, "untracked") for path in self.untracked]),
            "conflicted": rows([(path, "conflicted") for path in self.conflicted]),
        }


@dataclass
class Status:
    """The state of what has changed under a directory.  Paths are relative
    to it, with `/` between their parts; a directory ends in `/`."""

    states: dict[str, str] = field(default_factory=dict[str, str])
    ignored: set[str] = field(default_factory=set[str])
    # The directory itself, or one above it, is new to git or ignored by it:
    # then so is everything in it, and git lists nothing inside.
    all_untracked: bool = False
    all_ignored: bool = False
    # The repository as a whole, for the git panel (WORKSPACE 5.5).
    repository: Repository | None = None

    def _under(self, path: str, names: set[str] | dict[str, str]) -> str | None:
        """The entry of NAMES that is PATH or a directory above it."""
        parts = path.rstrip("/").split("/")
        for count in range(1, len(parts) + 1):
            above = "/".join(parts[:count])
            if count == len(parts) and above in names:
                return above
            if above + "/" in names:
                return above + "/"
        return None

    def is_ignored(self, path: str) -> bool:
        return self.all_ignored or self._under(path, self.ignored) is not None

    def of(self, path: str, directory: bool = False) -> str | None:
        """The state of the file PATH, or of the most pressing thing in the
        directory PATH; None when nothing there has changed."""
        if self.all_untracked:
            return "untracked"
        found = self._under(path, self.states)
        if found is not None:
            return self.states[found]  # itself, or a directory above that is all new
        if not directory:
            return None
        prefix = path.rstrip("/") + "/"
        inside = {state for name, state in self.states.items() if name.startswith(prefix)}
        return next((state for state in STATES if state in inside), None)


def parse(output: str, prefix: str = "", top: Path | None = None) -> Status:
    """Read `git status --porcelain=v2 --branch -z`.  Its paths are relative
    to the top of the repository.  The states of the result are of those
    under PREFIX, given relative to it; its `repository` is of all of them."""
    status = Status()
    repository = Repository(top if top is not None else Path())
    status.repository = repository
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        kind = record[:1]
        if kind == "#":
            _header(repository, record)
            continue
        if kind == "1":
            fields = record.split(" ", 8)
            path, xy = fields[-1], fields[1]
        elif kind == "2":
            fields = record.split(" ", 9)
            path, xy = fields[-1], fields[1]
            if index < len(records):  # the name it had before is a record of its own
                repository.renamed[path] = records[index]
            index += 1
        elif kind == "u":
            path, xy = record.split(" ", 10)[-1], "UU"
        elif kind in ("?", "!"):
            path, xy = record[2:], kind * 2
        else:
            continue  # the empty end
        if kind == "?":
            repository.untracked.append(path)
        elif kind == "u":
            repository.conflicted.append(path)
        elif kind in ("1", "2"):
            if xy[:1] != ".":
                repository.staged.append((path, _LETTERS.get(xy[:1], "modified")))
            if xy[1:2] != ".":
                repository.changes.append((path, _LETTERS.get(xy[1:2], "modified")))
        if path.endswith("/") and prefix.startswith(path):
            # A directory at or above PREFIX, listed whole.
            if kind == "!":
                status.all_ignored = True
            elif kind == "?":
                status.all_untracked = True
            continue
        if not path.startswith(prefix):
            continue
        path = path[len(prefix) :]
        if not path:
            continue
        if kind == "!":
            status.ignored.add(path)
        elif kind == "?":
            status.states[path] = "untracked"
        elif kind == "u":
            status.states[path] = "conflicted"
        elif xy[1:2] != ".":
            status.states[path] = "modified"  # changed since it was staged, or never staged
        else:
            status.states[path] = "staged"
    return status


def _header(repository: Repository, record: str) -> None:
    words = record.split(" ")
    if words[1:2] == ["branch.head"] and len(words) > 2:
        repository.branch = None if words[2] == "(detached)" else words[2]
    elif words[1:2] == ["branch.oid"] and len(words) > 2:
        repository.head = None if words[2] == "(initial)" else words[2]
    elif words[1:2] == ["branch.upstream"] and len(words) > 2:
        repository.upstream = words[2]
    elif words[1:2] == ["branch.ab"] and len(words) > 3:
        with contextlib.suppress(ValueError):
            repository.ahead, repository.behind = int(words[2]), -int(words[3])


async def status(directory: Path) -> Status | None:
    """What has changed under DIRECTORY, or None if it is not in a repository
    (or git is not to be had)."""
    try:
        where = await git(directory, "rev-parse", "--show-prefix", "--show-toplevel")
        if where.returncode != 0:
            return None
        listed = await git(
            directory, "status", "--porcelain=v2", "--branch", "-z",
            "--ignored=matching", "--untracked-files=all",
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - no git, no repository, no answer: no colours
        return None
    if listed.returncode != 0:
        return None
    prefix, _, top = where.stdout.rstrip("\n").partition("\n")
    return parse(listed.stdout, prefix, Path(top) if top else directory)


async def git(directory: Path, *args: str, input: str | None = None) -> proc.Completed:  # noqa: A002
    """Run git in DIRECTORY and return what it did.  It never waits for the
    user: a question it would ask is a failure."""
    return await proc.run(
        ["git", "-C", str(directory), *args], input=input, env=_ENV, timeout=_TIMEOUT
    )


async def show(directory: Path, what: str) -> str | None:
    """The text of WHAT, as `git show` names things: `:path` is the index's
    version of a file, `HEAD:path` the last commit's.  None if there is none."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(directory), "show", what,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **_ENV},
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - no git to run
        return None
    try:
        data, _ = await asyncio.wait_for(process.communicate(), _TIMEOUT)
    except TimeoutError:
        return None
    finally:
        # Timed out, or the command waiting was cancelled: git does not outlive it.
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())
    if process.returncode != 0:
        return None
    # Read as a file's bytes are, so that the two sides of a diff differ
    # only where the text does: line endings, a byte-order mark and the
    # encoding are treated alike.
    from neomacs.text.buffer import decode

    return decode(data)[0]
