"""Pausing the editor inside its own code (ENGINE 10.1 to 10.3).

A breakpoint stops the main thread inside the frame that hit it.  The editor
is then served by a second event loop on another thread, the debug loop,
until the user continues.  Exactly one of the two threads owns editor state
at any moment; `pause` and `_serve` are the only places it changes hands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
from types import CodeType, FrameType
from typing import TYPE_CHECKING, Any

from neomacs import _owner
from neomacs.errors import Quit

if TYPE_CHECKING:
    from neomacs.editor import Editor

mon = sys.monitoring
TOOL = mon.DEBUGGER_ID
E = mon.events
_STEP_EVENTS = E.LINE | E.PY_RETURN | E.PY_UNWIND | E.PY_YIELD
WIND_DOWN = 1.0  # seconds the debug loop is given to finish what it is doing

# Stepping never stops in the event loop's machinery or in the debugger.
_SKIP = (
    os.path.dirname(asyncio.__file__) + os.sep,
    os.path.dirname(__file__) + os.sep,
    "<frozen",
)

# The debugger of the engine whose main thread is the key.  Monitoring is
# global to the interpreter; callbacks find their debugger by thread and
# ignore every other thread, the debug loop's included.
_by_thread: dict[int, Debugger] = {}
_installed = False


class DebugAbort(Quit):
    """Raised in a paused frame by `debug.abort`, to unwind its command."""


def skipped(code: CodeType) -> bool:
    return code.co_filename.startswith(_SKIP)


class Debugger:
    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self.paused = False
        # True from the moment a pause is told to end until the main thread
        # has either paused again or got back to its event loop.  The editor
        # is between states then, and is not "idle".
        self.resuming = False
        self.reason = ""
        # While paused, or while inspecting a traceback: frames, innermost first,
        # each with the line it is at.
        self.stack: list[tuple[FrameType, int]] = []
        self.selected = 0
        self.inspecting = False  # looking at frames that have already unwound
        self.ui_frame: Any = None  # the editor frame (window) the pause is shown in
        self.keymap: Any = None  # the stepping keys; set by `neomacs.debug.ui`
        self.pauses = 0
        # Breakpoints: file name -> {line: condition or None}.
        self.breaks: dict[str, dict[int, str | None]] = {}
        # The code objects each file's breakpoints are in.
        self.resolved: dict[str, set[CodeType]] = {}
        self.on_raise: tuple[str, ...] = ()
        self.available = False
        # Stepping.
        self.mode: str | None = None  # "step", "next", "out"
        self.stopframe: FrameType | None = None
        self._local: dict[CodeType, int] = {}  # events set on each code object
        self._raised: BaseException | None = None
        # The debug loop.
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dispatcher: Any = None
        self._thread: threading.Thread | None = None
        self._go = threading.Event()
        self._resumed = threading.Event()
        self._resume: asyncio.Event | None = None
        self._action: str | None = None
        self._started = False
        # Input typed to the debugger too late for the pause it was typed in.
        # It goes to the next pause if the command stops again at once (keys
        # typed ahead while stepping), otherwise to the main loop.
        self._carried: list[Any] = []

    # -- what the modeline shows ------------------------------------------------

    @property
    def status(self) -> str:
        if self.paused:
            return "paused"
        return "inspecting" if self.inspecting else ""

    @property
    def frame(self) -> FrameType | None:
        """The selected stack frame, while paused or inspecting."""
        if (self.paused or self.inspecting) and self.stack:
            return self.stack[self.selected][0]
        return None

    # -- installing ---------------------------------------------------------------

    def install(self) -> None:
        """Called on the engine's main thread when it starts serving."""
        global _installed
        ident = threading.get_ident()
        if not _installed:
            try:
                mon.use_tool_id(TOOL, "neomacs")
            except ValueError:
                self.editor.message(
                    "Another debugger is attached to this process; breakpoints are unavailable",
                    kind="log",
                )
                return
            mon.register_callback(TOOL, E.LINE, _on_line)
            mon.register_callback(TOOL, E.PY_RETURN, _on_exit)
            mon.register_callback(TOOL, E.PY_UNWIND, _on_exit)
            mon.register_callback(TOOL, E.PY_YIELD, _on_yield)
            mon.register_callback(TOOL, E.RAISE, _on_raise)
            _installed = True
        _by_thread[ident] = self
        self.available = True
        from neomacs.debug import ui

        self.keymap = ui.DebugMode.keymap
        sys.breakpointhook = _breakpoint_hook
        from neomacs.live.evaluate import on_compile

        if self._code_compiled not in on_compile:
            on_compile.append(self._code_compiled)

    def uninstall(self) -> None:
        global _installed
        for ident, debugger in list(_by_thread.items()):
            if debugger is self:
                del _by_thread[ident]
        self.available = False
        from neomacs.live.evaluate import on_compile

        with contextlib.suppress(ValueError):
            on_compile.remove(self._code_compiled)
        if not _by_thread and _installed:
            with contextlib.suppress(ValueError):
                mon.set_events(TOOL, 0)
                for code in self._local:
                    mon.set_local_events(TOOL, code, 0)
                mon.free_tool_id(TOOL)
            _installed = False
            sys.breakpointhook = sys.__breakpointhook__
        self._local.clear()

    # -- monitoring ---------------------------------------------------------------

    def apply(self) -> None:
        """Bring the interpreter's event settings in line with the breakpoints
        and the stepping mode."""
        if not self.available:
            return
        wanted: dict[CodeType, int] = {}
        from neomacs.debug import breakpoints

        for code in breakpoints.codes_with_breaks(self):
            wanted[code] = E.LINE
        if self.stopframe is not None and self.mode in ("next", "out"):
            code = self.stopframe.f_code
            extra = _STEP_EVENTS if self.mode == "next" else E.PY_RETURN | E.PY_UNWIND
            wanted[code] = wanted.get(code, 0) | extra
        for code in list(self._local):
            if code not in wanted:
                mon.set_local_events(TOOL, code, 0)
                del self._local[code]
        for code, events in wanted.items():
            if self._local.get(code) != events:
                mon.set_local_events(TOOL, code, events)
                self._local[code] = events
        everywhere = (_STEP_EVENTS if self.mode == "step" else 0) | (
            E.RAISE if self.on_raise else 0
        )
        mon.set_events(TOOL, everywhere)
        mon.restart_events()  # lines switched off with DISABLE count again

    def _code_compiled(self, code: CodeType, filename: str) -> None:
        """The evaluation pipeline compiled CODE: its breakpoints apply before it runs."""
        from neomacs.debug import breakpoints

        if breakpoints.any_in(self, filename):
            breakpoints.refresh(self, extra=code)

    def _set_mode(self, mode: str | None, frame: FrameType | None = None) -> None:
        self.mode = mode
        self.stopframe = frame if mode is not None else None
        self.apply()

    def _descends(self, frame: FrameType | None) -> bool:
        """Whether FRAME is the frame being stepped, or was called from it."""
        target = self.stopframe
        for _ in range(200):
            if frame is None:
                return False
            if frame is target:
                return True
            frame = frame.f_back
        return False

    # -- pausing --------------------------------------------------------------------

    def can_pause(self) -> bool:
        ident = threading.get_ident()
        editor = self.editor
        return (
            not self.paused
            and ident == editor.main_thread
            and _owner.owner() == ident
            and editor.main_loop is not None
        )

    def pause(self, frame: FrameType, reason: str) -> None:
        """Stop here, on the main thread, until the user says otherwise.

        Returns when they continue or step.  Raises DebugAbort if they abort.
        """
        if not self.can_pause():
            return
        editor = self.editor
        from neomacs import editor as editor_module

        self.mode = None
        self.stopframe = None
        self.inspecting = False
        self.stack = build_stack(frame)
        self.selected = 0
        self.reason = reason
        self.ui_frame = editor_module.frame_var.get() or editor.frame
        self._action = None
        self.pauses += 1
        self.paused = True
        self._ensure_thread()
        assert self.loop is not None
        self._resumed.clear()
        previous_term: Any = None
        on_main = threading.current_thread() is threading.main_thread()
        if on_main:
            # The loop that would notice SIGTERM is this thread, which is about to block.
            with contextlib.suppress(ValueError, OSError):
                previous_term = signal.signal(signal.SIGTERM, self._on_sigterm)
        # ---- the hand-over: from here the debug thread owns the editor ----
        editor.activate(self.loop, self.dispatcher)
        self._go.set()
        self._resumed.wait()
        # ---- and here this thread owns it again ----
        if previous_term is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, previous_term)
        action = self._taken_action()
        self.stack = []
        editor.after_handover()
        assert editor.main_loop is not None
        editor.main_loop.call_soon(self._back_in_the_loop)
        if action == "abort":
            self._set_mode(None)
            raise DebugAbort("aborted from the debugger")
        if action in ("step", "next", "out"):
            self._set_mode(action, frame)
        else:
            self._set_mode(None)

    def _back_in_the_loop(self) -> None:
        if not self.paused:
            self.resuming = False
            self.editor.main_dispatcher.give(self._carried)
            self._carried = []

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        self.editor.stop()

    def _taken_action(self) -> str | None:
        """What the debug thread decided while this one was blocked."""
        return self._action

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        from neomacs.dispatch import Dispatcher

        self.loop = asyncio.new_event_loop()
        self.dispatcher = Dispatcher(self.editor)
        context = self.editor._context  # pyright: ignore[reportPrivateUsage]
        assert context is not None
        self._thread = threading.Thread(
            target=context.copy().run, args=(self._serve,), name="neomacs-debug-loop", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        """The debug thread: serve the editor for the length of each pause."""
        editor = self.editor
        assert self.loop is not None and editor.main_thread is not None
        while True:
            self._go.wait()
            self._go.clear()
            _owner.claim()
            try:
                self.loop.run_until_complete(self._session())
            except BaseException as exc:  # the main thread must be let go whatever happens
                with contextlib.suppress(Exception):
                    editor.message(f"The debugger failed: {type(exc).__name__}: {exc}", kind="log")
            finally:
                self.paused = False
                assert editor.main_loop is not None
                editor.activate(editor.main_loop, editor.main_dispatcher)
                _owner.give(editor.main_thread)
                self._resumed.set()

    async def _session(self) -> None:
        from neomacs import editor as editor_module
        from neomacs.debug import ui

        editor = self.editor
        if not self._started:
            self.dispatcher.start()
            self._started = True
        self._resume = asyncio.Event()
        self.resuming = False
        self.dispatcher.parked = False
        self.dispatcher.give(self._carried)
        self._carried = []
        editor_module.frame_var.set(self.ui_frame)
        editor.after_handover()
        editor.guard("showing the pause", ui.entered, self)
        try:
            await self._resume.wait()
        finally:
            self.paused = False
            editor.guard("leaving the pause", ui.left, self)
            await self._wind_down()

    async def _wind_down(self) -> None:
        """Let the debug loop finish what it is doing; nothing may run on it
        once the main thread has the editor back."""
        loop = asyncio.get_running_loop()
        dispatcher = self.dispatcher
        for answer in list(dispatcher._answers):  # commands waiting on a prompt  # noqa: SLF001
            if not answer.done():
                answer.set_exception(Quit("the paused command was resumed"))
        me = asyncio.current_task()
        deadline = loop.time() + WIND_DOWN
        while True:
            await asyncio.sleep(0)
            others = [
                t
                for t in asyncio.all_tasks()
                if t is not me and t is not dispatcher.worker and not t.done()
            ]
            if not others and not dispatcher.busy:
                break
            if loop.time() > deadline:
                for task in others:
                    task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                break
            await asyncio.sleep(0.001)
        self._carried = dispatcher.take_pending()

    def resume(self, action: str) -> None:
        """Called on the debug loop: end the pause with ACTION."""
        self._action = action
        self.resuming = True
        self.dispatcher.parked = True  # what is typed from here on waits for the next owner
        if self._resume is not None:
            self._resume.set()

    def resume_from_anywhere(self, action: str) -> None:
        if not self.paused or self.loop is None:
            return
        self._action = action
        self.resuming = True
        self.dispatcher.parked = True
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(lambda: self._resume is not None and self._resume.set())

    # -- looking at frames that have unwound (ENGINE 10.5) -------------------------

    def inspect(self, exc: BaseException) -> bool:
        frames: list[tuple[FrameType, int]] = []
        tb = exc.__traceback__
        while tb is not None:
            if not skipped(tb.tb_frame.f_code):
                frames.append((tb.tb_frame, tb.tb_lineno))
            tb = tb.tb_next
        if not frames:
            return False
        frames.reverse()
        self.stack = frames
        self.selected = 0
        self.inspecting = True
        self.reason = f"{type(exc).__name__}: {exc}"
        return True

    def stop_inspecting(self) -> None:
        self.inspecting = False
        self.stack = []


def build_stack(frame: FrameType | None) -> list[tuple[FrameType, int]]:
    stack: list[tuple[FrameType, int]] = []
    while frame is not None:
        if not skipped(frame.f_code):
            stack.append((frame, frame.f_lineno))
        frame = frame.f_back
    return stack


# -- callbacks: these run inside whatever code is being monitored ---------------------


def _on_line(code: CodeType, line: int) -> Any:
    debugger = _by_thread.get(threading.get_ident())
    if debugger is None or debugger.paused:
        return None
    mode = debugger.mode
    frame = sys._getframe(1)  # pyright: ignore[reportPrivateUsage]
    reason: str | None = None
    if mode == "next":
        if frame is debugger.stopframe:
            reason = "step"
    elif mode == "step" and not skipped(code) and debugger._descends(frame):  # pyright: ignore[reportPrivateUsage]
        reason = "step"
    if reason is None:
        lines = debugger.breaks.get(code.co_filename)
        if lines is None or line not in lines:
            # No breakpoint on this line: stop hearing about it until something changes.
            return mon.DISABLE if mode is None else None
        condition = lines[line]
        if condition:
            try:
                if not eval(condition, frame.f_globals, frame.f_locals):
                    return None
            except Exception as exc:
                reason = f"breakpoint (its condition failed: {type(exc).__name__}: {exc})"
        reason = reason or "breakpoint"
    debugger.pause(frame, reason)
    return None


def _on_exit(code: CodeType, offset: int, value: Any) -> Any:
    """The frame being stepped is returning or unwinding: carry on in its caller."""
    debugger = _by_thread.get(threading.get_ident())
    if debugger is None or debugger.mode is None:
        return None
    frame = sys._getframe(1)  # pyright: ignore[reportPrivateUsage]
    if frame is not debugger.stopframe:
        return None
    caller = frame.f_back
    if caller is None or skipped(caller.f_code):
        debugger._set_mode(None)  # back in the event loop: the step is over  # pyright: ignore[reportPrivateUsage]
    else:
        debugger._set_mode("next", caller)  # pyright: ignore[reportPrivateUsage]
    return None


def _on_yield(code: CodeType, offset: int, value: Any) -> Any:
    """The frame being stepped is suspending at an `await`.  The world runs on;
    the pause comes when this frame does (ENGINE 10.3)."""
    debugger = _by_thread.get(threading.get_ident())
    if debugger is None or debugger.mode != "step":
        return None
    frame = sys._getframe(1)  # pyright: ignore[reportPrivateUsage]
    if frame is debugger.stopframe:
        debugger._set_mode("next", frame)  # pyright: ignore[reportPrivateUsage]
    return None


def _on_raise(code: CodeType, offset: int, exc: BaseException) -> Any:
    debugger = _by_thread.get(threading.get_ident())
    if debugger is None or not debugger.on_raise or debugger.paused:
        return None
    if exc is debugger._raised or skipped(code):  # pyright: ignore[reportPrivateUsage]
        return None
    names = {klass.__name__ for klass in type(exc).__mro__}
    if names.isdisjoint(debugger.on_raise):
        return None
    debugger._raised = exc  # pyright: ignore[reportPrivateUsage]
    debugger.pause(sys._getframe(1), f"{type(exc).__name__} raised: {exc}")  # pyright: ignore[reportPrivateUsage]
    return None


def _breakpoint_hook(*args: Any, **kwargs: Any) -> Any:
    debugger = _by_thread.get(threading.get_ident())
    if debugger is None or not debugger.can_pause():
        return None  # not on an engine's main thread, or already paused
    debugger.pause(sys._getframe(1), "breakpoint()")  # pyright: ignore[reportPrivateUsage]
    return None
