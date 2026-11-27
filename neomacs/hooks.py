"""Hooks: named lists of functions, global and per buffer (ENGINE 7.4)."""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable
from typing import Any

from neomacs.ownership import ownership
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

    def __init__(self, name: str, doc: str = "", *, register: bool = True) -> None:
        self.name = name
        self.doc = doc
        self._registered = register
        self._global: dict[tuple[str, str], HookFn] = {}
        self._local: weakref.WeakKeyDictionary[Any, dict[tuple[str, str], HookFn]] = (
            weakref.WeakKeyDictionary()
        )
        if register:
            hooks[name] = self

    @provisional("0.1")
    def add[F: HookFn](self, fn: F, *, buffer: Any = None) -> F:
        """Add FN, globally or for one buffer.  Usable as a decorator."""
        table = self._global if buffer is None else self._local.setdefault(buffer, {})
        key = _key(fn)
        table[key] = fn
        if buffer is None and self._registered:
            ownership.record(key[0], "hook", (self.name, key), lambda: self._global.pop(key, None))
        return fn

    @provisional("0.1")
    def remove(self, fn: HookFn, *, buffer: Any = None) -> None:
        table = self._global if buffer is None else self._local.get(buffer, {})
        key = _key(fn)
        table.pop(key, None)
        if buffer is None:
            ownership.forget("hook", (self.name, key))

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
                result = fn(*args)
                if inspect.iscoroutine(result):
                    result.close()
                    raise TypeError(
                        f"hook {self.name} is run synchronously; {_key(fn)[1]} is a coroutine"
                    )
            except Exception as exc:
                _report(self.name, fn, exc)

    @provisional("0.1")
    async def arun(self, *args: Any, buffer: Any = None) -> None:
        """Like `run`, but a function that returns an awaitable is awaited, in order."""
        for fn in self.functions(buffer):
            try:
                result = fn(*args)
                if inspect.isawaitable(result):
                    await result
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

pre_command = Hook("pre_command", "Run before each command, with (command).")
post_command = Hook("post_command", "Run after each command, with (command).")
mode_enabled = Hook("mode_enabled", "Run when a mode is enabled in a buffer, with (buffer, mode).")
window_changed = Hook("window_changed", "Run when a frame's focused view changes, with (frame).")
ui_attached = Hook("ui_attached", "Run when a frontend attaches, with (frame).")
