"""Hooks: named lists of functions, global and per buffer (ENGINE 7.4)."""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any

from neomacs.stability import provisional

HookFn = Callable[..., Any]


def _key(fn: HookFn) -> tuple[str, str]:
    return (getattr(fn, "__module__", "") or "", getattr(fn, "__qualname__", repr(fn)))


@provisional("0.1")
class Hook:
    """A named, documented list of functions.

    Adding is idempotent, keyed on the function's module and qualified name,
    so re-evaluating a definition replaces it instead of adding a duplicate.
    """

    def __init__(self, name: str, doc: str = "") -> None:
        self.name = name
        self.doc = doc
        self._global: dict[tuple[str, str], HookFn] = {}
        self._local: weakref.WeakKeyDictionary[Any, dict[tuple[str, str], HookFn]] = (
            weakref.WeakKeyDictionary()
        )
        hooks[name] = self

    @provisional("0.1")
    def add[F: HookFn](self, fn: F, *, buffer: Any = None) -> F:
        """Add FN, globally or for one buffer.  Usable as a decorator."""
        table = self._global if buffer is None else self._local.setdefault(buffer, {})
        table[_key(fn)] = fn
        return fn

    @provisional("0.1")
    def remove(self, fn: HookFn, *, buffer: Any = None) -> None:
        table = self._global if buffer is None else self._local.get(buffer, {})
        table.pop(_key(fn), None)

    @provisional("0.1")
    def functions(self, buffer: Any = None) -> list[HookFn]:
        """The functions that would run for BUFFER: global ones, then its own."""
        fns = list(self._global.values())
        if buffer is not None:
            fns.extend(self._local.get(buffer, {}).values())
        return fns

    @provisional("0.1")
    def run(self, *args: Any, buffer: Any = None) -> None:
        """Call every function with ARGS.  One that raises is reported, and the rest still run."""
        for fn in self.functions(buffer):
            try:
                fn(*args)
            except Exception as exc:
                _report(self.name, fn, exc)

    def __repr__(self) -> str:
        return f"<Hook {self.name}>"


def _report(hook: str, fn: HookFn, exc: Exception) -> None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    if editor is None:
        raise exc
    editor.report_exception(f"in hook {hook} ({_key(fn)[1]})", exc)


hooks: dict[str, Hook] = {}
