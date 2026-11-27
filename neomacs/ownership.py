"""Which module made each registration (ENGINE D30).

Commands, options, hook functions, key bindings, menu items and the rest
record their owner here.  Re-evaluating a module can then drop what it no
longer defines, and an extension can be disabled without a trace.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from neomacs.stability import provisional

Remover = Callable[[], Any]


@provisional("0.1")
class Ownership:
    def __init__(self) -> None:
        # owner module -> {(kind, key): how to undo the registration}
        self._records: dict[str, dict[tuple[str, Any], Remover]] = {}
        self._owners: dict[tuple[str, Any], str] = {}

    @provisional("0.1")
    def record(self, owner: str, kind: str, key: Any, remove: Remover) -> None:
        """Note that OWNER registered KEY in the registry called KIND."""
        previous = self._owners.get((kind, key))
        if previous is not None and previous != owner:
            self._records.get(previous, {}).pop((kind, key), None)
        self._owners[(kind, key)] = owner
        self._records.setdefault(owner, {})[(kind, key)] = remove

    @provisional("0.1")
    def forget(self, kind: str, key: Any) -> None:
        """A registration was removed by other means."""
        owner = self._owners.pop((kind, key), None)
        if owner is not None:
            self._records.get(owner, {}).pop((kind, key), None)

    @provisional("0.1")
    def owner_of(self, kind: str, key: Any) -> str | None:
        return self._owners.get((kind, key))

    @provisional("0.1")
    def owned(self, owner: str) -> list[tuple[str, Any]]:
        """Everything OWNER has registered, as (kind, key) pairs."""
        return list(self._records.get(owner, {}))

    @provisional("0.1")
    def release(
        self, owner: str, *, keep: set[tuple[str, Any]] | None = None
    ) -> list[tuple[str, Any]]:
        """Undo OWNER's registrations, except those in KEEP.  Returns what was removed."""
        removed: list[tuple[str, Any]] = []
        for item, remove in list(self._records.get(owner, {}).items()):
            if keep is not None and item in keep:
                continue
            self._records[owner].pop(item, None)
            self._owners.pop(item, None)
            remove()
            removed.append(item)
        return removed


ownership = Ownership()


def caller_module(depth: int = 2) -> str:
    """The name of the module DEPTH frames up the stack."""
    try:
        return str(sys._getframe(depth).f_globals.get("__name__", "?"))  # pyright: ignore[reportPrivateUsage]
    except ValueError:
        return "?"
