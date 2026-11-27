"""Completion (ENGINE 7.5): asynchronous sources of candidates."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neomacs.stability import provisional


@provisional("0.1")
@dataclass(frozen=True)
class Candidate:
    text: str  # what accepting the candidate puts in place of the input
    annotation: str = ""  # shown beside it


@provisional("0.1")
class Completer(Protocol):
    async def candidates(self, text: str, pos: int) -> list[Candidate]: ...


@provisional("0.1")
def matches(candidate: str, text: str, style: str = "substring") -> bool:
    """Whether CANDIDATE matches what was typed: by "prefix", "substring" or "fuzzy"."""
    c, t = candidate.casefold(), text.casefold()
    if style == "prefix":
        return c.startswith(t)
    if style == "fuzzy":
        at = 0
        for ch in t:
            at = c.find(ch, at) + 1
            if at == 0:
                return False
        return True
    return all(part in c for part in t.split())


@provisional("0.1")
def rank(items: Iterable[Candidate], text: str, style: str = "substring") -> list[Candidate]:
    """The items that match TEXT, best first: exact, then prefix, then the rest."""
    folded = text.casefold()

    def key(candidate: Candidate) -> tuple[int, int, str]:
        c = candidate.text.casefold()
        return (0 if c == folded else 1 if c.startswith(folded) else 2, len(c), c)

    return sorted((c for c in items if matches(c.text, text, style)), key=key)


@provisional("0.1")
class FunctionCompleter:
    """A completer from a function of the text typed so far.  The function may be async."""

    def __init__(
        self, fn: Callable[[str], Iterable[Candidate | str] | Awaitable[Iterable[Candidate | str]]]
    ) -> None:
        self._fn = fn

    async def candidates(self, text: str, pos: int) -> list[Candidate]:
        result = self._fn(text)
        if isinstance(result, Awaitable):
            result = await result
        return [c if isinstance(c, Candidate) else Candidate(c) for c in result]


@provisional("0.1")
def choices(items: Iterable[str], style: str = "substring") -> FunctionCompleter:
    """A completer over a fixed set of strings."""
    fixed = [Candidate(item) for item in items]
    return FunctionCompleter(lambda text: rank(fixed, text, style))


def _commands(text: str) -> list[Candidate]:
    from neomacs.commands import commands

    return rank((Candidate(c.name, c.summary) for c in commands.values()), text)


def _buffers(text: str) -> list[Candidate]:
    from neomacs.editor import current

    return rank((Candidate(b.name) for b in current().buffers), text)


def _files(text: str) -> list[Candidate]:
    expanded = os.path.expanduser(text)
    directory, partial = os.path.split(expanded)
    try:
        entries = sorted(os.scandir(directory or "."), key=lambda e: e.name)
    except OSError:
        return []
    found: list[Candidate] = []
    for entry in entries:
        if entry.name.startswith(".") and not partial.startswith("."):
            continue
        if matches(entry.name, partial, "prefix") or matches(entry.name, partial):
            is_dir = entry.is_dir()
            path = str(Path(directory or ".") / entry.name) if directory else entry.name
            found.append(Candidate(path + ("/" if is_dir else ""), "directory" if is_dir else ""))
    found.sort(key=lambda c: (not os.path.basename(c.text.rstrip("/")).startswith(partial), c.text))
    return found


commands_completer = FunctionCompleter(_commands)
buffers_completer = FunctionCompleter(_buffers)
files_completer = FunctionCompleter(_files)
