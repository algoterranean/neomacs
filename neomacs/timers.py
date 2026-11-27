"""Timers (ENGINE 11): call a function later, repeatedly, or when the editor is idle.

Timers run on the engine's main loop, on the thread that owns the editor,
through the error wrapper: a function that raises is reported and the editor
carries on.  While the debugger has a command paused the main loop is not
running, so timers wait until it resumes (ENGINE 10.2).
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import itertools
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from neomacs.ownership import ownership
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.editor import Editor

_ids = itertools.count(1)


@provisional("0.1")
class Timer:
    """A timer that has been set.  `cancel()` stops it."""

    def __init__(
        self,
        timers: Timers,
        kind: str,
        seconds: float,
        fn: Callable[[], Any],
        owner: str,
        key: Any,
        context: contextvars.Context,
    ) -> None:
        self.kind = kind  # "after", "every" or "idle"
        self.seconds = seconds
        self.fn = fn
        self.owner = owner
        self.key = key
        self.active = True
        self._timers = timers
        self._context = context
        self._handle: asyncio.TimerHandle | None = None
        self._due = 0.0  # when a repeating timer is next meant to run

    @property
    def name(self) -> str:
        return str(getattr(self.fn, "__qualname__", None) or repr(self.fn))

    @provisional("0.1")
    def cancel(self) -> None:
        """Stop the timer.  A function already running is not interrupted."""
        if not self.active:
            return
        self.active = False
        self._timers.drop(self)

    def __repr__(self) -> str:
        state = "" if self.active else " cancelled"
        return f"<Timer {self.kind} {self.seconds:g}s {self.name}{state}>"


class Timers:
    """One editor's timers."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self._all: dict[Any, Timer] = {}
        self._idle: list[Timer] = []
        self._waiting: list[tuple[Timer, float]] = []  # set before the loop was running

    def __iter__(self):  # noqa: ANN204
        return iter(list(self._all.values()))

    def __len__(self) -> int:
        return len(self._all)

    # -- setting --------------------------------------------------------------

    def add(self, kind: str, seconds: float, fn: Callable[[], Any], owner: str) -> Timer:
        if not callable(fn):
            raise TypeError(f"a timer needs a function to call, not {fn!r}")
        seconds = float(seconds)
        if seconds < 0 or (kind == "every" and seconds <= 0):
            raise ValueError(f"a timer cannot be set for {seconds} seconds")
        key = self._key(kind, fn)
        previous = self._all.get(key)
        if previous is not None:
            previous.cancel()  # the same function set again: this replaces it
        from neomacs.editor import pins_var

        context = contextvars.copy_context()
        context.run(pins_var.set, None)  # the frame is inherited, the view is not (ENGINE 7.7)
        timer = Timer(self, kind, seconds, fn, owner, key, context)
        self._all[key] = timer
        ownership.record(owner, "timer", key, timer.cancel)
        if kind == "idle":
            self._idle.append(timer)
        # An idle timer counts from now, however long the user has been away
        # already: the pause it runs after is one that begins once it is set.
        delay = seconds
        timer._due = time.monotonic() + delay  # pyright: ignore[reportPrivateUsage]
        self._arm(timer, delay)
        return timer

    @staticmethod
    def _key(kind: str, fn: Callable[[], Any]) -> Any:
        """What identifies a timer.  A repeating timer is known by its function,
        as a hook function is, so that evaluating the line that sets it a second
        time replaces it.  A function with no name of its own cannot be told
        from another, and a one-shot timer may properly be set many times."""
        module = getattr(fn, "__module__", None)
        qualname = getattr(fn, "__qualname__", None)
        if kind == "after" or not module or not qualname or "<" in qualname:
            return (kind, next(_ids))
        return (kind, module, qualname)

    def drop(self, timer: Timer) -> None:
        if self._all.get(timer.key) is timer:
            del self._all[timer.key]
            ownership.forget("timer", timer.key)
        if timer in self._idle:
            self._idle.remove(timer)
        handle, timer._handle = timer._handle, None  # pyright: ignore[reportPrivateUsage]
        if handle is not None:
            self._on_loop(handle.cancel)

    # -- running --------------------------------------------------------------

    def _on_loop(self, fn: Callable[[], Any]) -> None:
        """Call FN on the main loop: now if this is its thread, otherwise soon."""
        loop = self.editor.main_loop
        if loop is None or loop.is_closed():
            return
        if threading.get_ident() == self.editor.main_thread:
            fn()
        else:
            loop.call_soon_threadsafe(fn)

    def _arm(self, timer: Timer, delay: float) -> None:
        loop = self.editor.main_loop
        if loop is None:
            self._waiting.append((timer, delay))
            return

        def arm() -> None:
            if timer.active and not loop.is_closed():
                timer._handle = loop.call_later(delay, self._fire, timer)  # pyright: ignore[reportPrivateUsage]

        self._on_loop(arm)

    def start(self) -> None:
        """The main loop is running: set the timers that were asked for before it was."""
        waiting, self._waiting = self._waiting, []
        for timer, delay in waiting:
            self._arm(timer, delay)

    def _fire(self, timer: Timer) -> None:
        timer._handle = None  # pyright: ignore[reportPrivateUsage]
        if not timer.active:
            return
        if timer.kind == "after":
            timer.active = False
            self.drop(timer)
        elif timer.kind == "every":
            # Keep to the rhythm; if the loop was held up, skip what was missed.
            now = time.monotonic()
            due = timer._due + timer.seconds  # pyright: ignore[reportPrivateUsage]
            if due <= now:
                due = now + timer.seconds
            timer._due = due  # pyright: ignore[reportPrivateUsage]
            self._arm(timer, due - now)
        timer._context.run(self._call, timer)  # pyright: ignore[reportPrivateUsage]

    def _call(self, timer: Timer) -> None:
        editor = self.editor
        where = f"in timer {timer.name}"
        result = editor.guard(where, timer.fn)
        if inspect.iscoroutine(result):
            editor.start_task(result, name=f"timer {timer.name}")
        editor.update_frames()

    def activity(self) -> None:
        """The user did something: idle timers start counting again."""
        for timer in self._idle:
            handle, timer._handle = timer._handle, None  # pyright: ignore[reportPrivateUsage]
            if handle is not None:
                handle.cancel()
            self._arm(timer, timer.seconds)

    def close(self) -> None:
        for timer in list(self._all.values()):
            timer.cancel()
