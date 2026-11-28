"""What git says about the files of a directory (WORKSPACE 5.4, 5.5).

Git is run for this, unlike the branch name in `neomacs.vcs`, which is read
from a file because it is asked for on every update.  Nothing here is asked
for that often: a listing is read when the file tree is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from neomacs import proc

# The states a file can be in, the most pressing first: a directory takes the
# most pressing state of anything inside it.
STATES = ("conflicted", "modified", "staged", "untracked")
_TIMEOUT = 10.0
# Looking must not write to the repository, nor wait for a prompt.
_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


@dataclass
class Status:
    """The state of what has changed under a directory.  Paths are relative
    to it, with `/` between their parts; a directory ends in `/`."""

    states: dict[str, str] = field(default_factory=dict[str, str])
    ignored: set[str] = field(default_factory=set[str])

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
        return self._under(path, self.ignored) is not None

    def of(self, path: str, directory: bool = False) -> str | None:
        """The state of the file PATH, or of the most pressing thing in the
        directory PATH; None when nothing there has changed."""
        found = self._under(path, self.states)
        if found is not None:
            return self.states[found]  # itself, or a directory above that is all new
        if not directory:
            return None
        prefix = path.rstrip("/") + "/"
        inside = {state for name, state in self.states.items() if name.startswith(prefix)}
        return next((state for state in STATES if state in inside), None)


def parse(output: str, prefix: str = "") -> Status:
    """Read `git status --porcelain=v2 -z`.  Its paths are relative to the
    top of the repository; those not under PREFIX are left out, and the rest
    are given relative to it."""
    status = Status()
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        kind = record[:1]
        if kind == "1":
            fields = record.split(" ", 8)
            path, xy = fields[-1], fields[1]
        elif kind == "2":
            fields = record.split(" ", 9)
            path, xy = fields[-1], fields[1]
            index += 1  # the name it had before is a record of its own
        elif kind == "u":
            path, xy = record.split(" ", 10)[-1], "UU"
        elif kind in ("?", "!"):
            path, xy = record[2:], kind * 2
        else:
            continue  # a header, or the empty end
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


async def status(directory: Path) -> Status | None:
    """What has changed under DIRECTORY, or None if it is not in a repository
    (or git is not to be had)."""
    base = ["git", "-C", str(directory)]
    try:
        where = await proc.run([*base, "rev-parse", "--show-prefix"], env=_ENV, timeout=_TIMEOUT)
        if where.returncode != 0:
            return None
        asked = ["status", "--porcelain=v2", "-z", "--ignored=matching", "--untracked-files=normal"]
        listed = await proc.run([*base, *asked], env=_ENV, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001 - no git, no repository, no answer: no colours
        return None
    if listed.returncode != 0:
        return None
    return parse(listed.stdout, where.stdout.strip("\n"))
