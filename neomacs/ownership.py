"""Which module made each registration (ENGINE D30).

Commands, options, hook functions, key bindings, menu items and the rest
record their owner here.  Re-evaluating a module can then drop what it no
longer defines, and an extension can be disabled without a trace.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Generator
from typing import Any

from neomacs.stability import provisional

Remover = Callable[[], Any]


@provisional("0.1")
class Ownership:
    def __init__(self) -> None:
        # owner module -> {(kind, key): (how to undo the registration, whether it
        # was made while the module was being loaded or evaluated)}
        self._records: dict[str, dict[tuple[str, Any], tuple[Remover, bool]]] = {}
        self._owners: dict[tuple[str, Any], str] = {}
        # While a module is being re-evaluated: what it has registered this time.
        self._tracking: dict[str, set[tuple[str, Any]]] = {}

    @provisional("0.1")
    def record(self, owner: str, kind: str, key: Any, remove: Remover) -> None:
        """Note that OWNER registered KEY in the registry called KIND."""
        made = self._tracking.get(owner)
        if made is not None:
            made.add((kind, key))
        previous = self._owners.get((kind, key))
        if previous is not None and previous != owner:
            self._records.get(previous, {}).pop((kind, key), None)
        self._owners[(kind, key)] = owner
        self._records.setdefault(owner, {})[(kind, key)] = (
            remove,
            made is not None or _loading(owner),
        )

    @contextlib.contextmanager
    def tracking(self, owner: str) -> Generator[set[tuple[str, Any]]]:
        """Collect what OWNER registers during a block.  Afterwards
        `release(owner, keep=...)` removes what it used to register and no longer does."""
        outer = self._tracking.get(owner)
        made: set[tuple[str, Any]] = set()
        self._tracking[owner] = made
        try:
            yield made
        finally:
            if outer is None:
                del self._tracking[owner]
            else:
                outer.update(made)
                self._tracking[owner] = outer

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
        """Undo OWNER's registrations.  Returns what was removed.

        With KEEP, which is what a re-evaluation of the module just registered,
        only registrations made by loading the module are candidates: one that
        a function of the module made later, when it was called, is not
        something evaluating the module again would have repeated.
        """
        removed: list[tuple[str, Any]] = []
        for item, (remove, at_load) in list(self._records.get(owner, {}).items()):
            if keep is not None and (item in keep or not at_load):
                continue
            self._records[owner].pop(item, None)
            self._owners.pop(item, None)
            remove()
            removed.append(item)
        return removed


def _loading(owner: str) -> bool:
    """Whether the module called OWNER is in the middle of being imported."""
    spec = getattr(sys.modules.get(owner), "__spec__", None)
    return bool(getattr(spec, "_initializing", False))


ownership = Ownership()


def caller_module(depth: int = 2) -> str:
    """The name of the module DEPTH frames up the stack."""
    try:
        return str(sys._getframe(depth).f_globals.get("__name__", "?"))  # pyright: ignore[reportPrivateUsage]
    except ValueError:
        return "?"
