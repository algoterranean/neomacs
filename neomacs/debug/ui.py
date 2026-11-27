"""The debugger's interface (ENGINE 10.4, 10.5): where it shows the pause, and its commands."""

from __future__ import annotations

import reprlib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from neomacs.commands import RawPrefix, command
from neomacs.debug import breakpoints
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap, global_map
from neomacs.live import run as live
from neomacs.live import source
from neomacs.modes import Mode
from neomacs.options import Option, options

if TYPE_CHECKING:
    from neomacs.debug.pause import Debugger
    from neomacs.text.buffer import Buffer

LINE_LAYER = "debug-line"
_repr = reprlib.Repr(maxstring=80, maxother=80, maxlist=8, maxdict=8, maxtuple=8)

Option(
    "debug_on_raise",
    str,
    "",
    "Pause where an exception of this type is raised, caught or not.  Several "
    "names may be given, separated by spaces.  Empty for never.",
)


class DebugMode(Mode):
    """A minor mode on the buffers that show paused code.  Its keys step and continue."""

    name = "debug"
    minor = True
    keymap = Keymap("debug")


class DebugBufferMode(live.SpecialMode):
    name = "debug-stack"
    keymap = Keymap("debug-stack")


assert DebugMode.keymap is not None and DebugBufferMode.keymap is not None
for _key, _name in [
    ("<f5>", "debug.continue"),
    ("<f10>", "debug.next"),
    ("<f11>", "debug.step"),
    ("S-<f11>", "debug.out"),
    ("C-c u", "debug.up"),
    ("C-c d", "debug.down"),
    ("C-c s", "debug.show"),
    ("C-c a", "debug.abort"),
]:
    DebugMode.keymap.bind(_key, _name)
    DebugBufferMode.keymap.bind(_key, _name)
global_map.bind("<f9>", "debug.toggle_breakpoint")
global_map.bind("<gutter-mouse-1>", "debug.toggle_breakpoint_at_mouse")


def _debugger() -> Debugger:
    return ed.debugger


def _on_raise_changed(value: Any, buffer: Any = None) -> None:
    debugger = _debugger()
    debugger.on_raise = tuple(str(value).split())
    debugger.apply()


options["debug_on_raise"].on_change.add(_on_raise_changed)


# -- showing where we are ---------------------------------------------------------


def _buffer_for(filename: str) -> Buffer | None:
    name = source.buffer_name(filename)
    if name is not None:
        return ed.buffers.get(name)
    return ed.buffers.visit(filename) if Path(filename).exists() else None


def _clear_marks(debugger: Debugger) -> None:
    for buffer in ed.buffers:
        if buffer.locals.pop("_debug_shown", None):
            buffer.layer(LINE_LAYER).clear()
            buffer.minor_modes[:] = [m for m in buffer.minor_modes if not isinstance(m, DebugMode)]


def show_frame(debugger: Debugger) -> None:
    """Show the selected stack frame's line, marked, in the window."""
    _clear_marks(debugger)
    write_stack(debugger)
    if not debugger.stack:
        return
    frame, line = debugger.stack[debugger.selected]
    code = frame.f_code
    where = f"{Path(code.co_filename).name}, line {line}, in {code.co_name}"
    buffer = _buffer_for(code.co_filename)
    state = "Paused" if debugger.paused else "Inspecting"
    if buffer is None:
        ed.message(f"{state} ({debugger.reason}) at {where}; its source is not available")
        return
    line = max(1, min(buffer.line_count, line))
    start = buffer.line_start(line)
    buffer.layer(LINE_LAYER).add(
        start,
        buffer.line_end(line),
        priority=50,
        face="debug-line",
        gutter={"column": "signs", "text": "▶", "face": "warning", "priority": 60},
    )
    buffer.locals["_debug_shown"] = True
    buffer.minor_modes.append(DebugMode())
    view = ed.show(buffer, focus=True)
    view.selections.set([(start, start)])
    view.reveal(start, "center")
    ed.message(f"{state} ({debugger.reason}) at {where}")


def write_stack(debugger: Debugger) -> None:
    """Fill the `*debug*` buffer: the stack, and the selected frame's variables."""
    buffer = live.special_buffer("*debug*", DebugBufferMode)
    state = "Paused" if debugger.paused else "Inspecting" if debugger.inspecting else "Not paused"
    buffer.insert(0, f"{state}: {debugger.reason}\n\nStack, innermost first:\n")
    links = buffer.layer("links")
    for index, (frame, line) in enumerate(debugger.stack):
        code = frame.f_code
        start = len(buffer)
        mark = ">" if index == debugger.selected else " "
        buffer.insert(start, f"{mark} {code.co_name}  {Path(code.co_filename).name}:{line}\n")
        links.add(start + 2, len(buffer) - 1, run=lambda index=index: select(index))
    frame = debugger.frame
    if frame is not None:
        buffer.insert(len(buffer), f"\nVariables of {frame.f_code.co_name}:\n")
        for name, value in frame.f_locals.items():
            try:
                shown = _repr.repr(value)
            except Exception as exc:
                shown = f"<repr failed: {type(exc).__name__}>"
            buffer.insert(len(buffer), f"  {name} = {shown}\n")
    live.finish_special(buffer)


def entered(debugger: Debugger) -> None:
    """Called on the debug loop when a pause begins."""
    frame = debugger.ui_frame
    if frame is not None and frame.id in ed.frames:
        # The paused command expects its window to show the buffer it was
        # working on; that is put back when it resumes.
        frame.locals_before_pause = (frame.window, frame.window.buffer, frame.focus)
    show_frame(debugger)


def left(debugger: Debugger) -> None:
    """Called on the debug loop when a pause ends."""
    _clear_marks(debugger)
    frame = debugger.ui_frame
    before = getattr(frame, "locals_before_pause", None)
    if frame is not None and before is not None and frame.id in ed.frames:
        window, buffer, focus = before
        if window.id in frame.views and ed.buffers.get(buffer.name) is buffer:
            window.show(buffer)
        if focus.id in frame.views:
            frame.focus_view(focus)
        frame.locals_before_pause = None
    for prompt_frame in list(ed.frames.values()):
        prompt_frame.schedule_update()


def select(index: int) -> None:
    debugger = _debugger()
    if not debugger.stack:
        raise NeomacsError("Nothing is paused")
    debugger.selected = max(0, min(len(debugger.stack) - 1, index))
    show_frame(debugger)


# -- commands -------------------------------------------------------------------------


def _resume(action: str) -> None:
    debugger = _debugger()
    if debugger.paused:
        debugger.resume(action)
    elif debugger.inspecting:
        if action != "continue":
            raise NeomacsError("This code has finished running; it can be looked at, not stepped")
        debugger.stop_inspecting()
        _clear_marks(debugger)
        write_stack(debugger)
        ed.message("Stopped inspecting")
    else:
        raise NeomacsError("Nothing is paused")


@command(name="debug.continue")
def continue_() -> None:
    """Let the paused command run on, to the next breakpoint if it meets one."""
    _resume("continue")


@command(name="debug.step")
def step() -> None:
    """Run to the next line, going into any function that is called."""
    _resume("step")


@command(name="debug.next")
def next_() -> None:
    """Run to the next line of this function, stepping over calls."""
    _resume("next")


@command(name="debug.out")
def out() -> None:
    """Run until this function returns to its caller."""
    _resume("out")


@command(name="debug.abort")
def abort() -> None:
    """Abandon the paused command: an exception unwinds it from where it stopped."""
    _resume("abort")


@command(name="debug.up")
def up() -> None:
    """Select the caller of the selected stack frame."""
    select(_debugger().selected + 1)


@command(name="debug.down")
def down() -> None:
    """Select the frame that the selected one called."""
    select(_debugger().selected - 1)


@command(name="debug.show")
def show() -> None:
    """Show the stack and the selected frame's variables."""
    debugger = _debugger()
    write_stack(debugger)
    buffer = ed.buffers.get("*debug*")
    assert buffer is not None
    view = ed.show(buffer, "help", focus=True)
    view.selections.set([(0, 0)])


@command(name="debug.toggle_breakpoint")
async def toggle_breakpoint(prefix: Annotated[Any, RawPrefix] = None) -> None:
    """Set or remove a breakpoint on the cursor's line.  With a prefix
    argument, ask for a condition: the breakpoint stops only when it is true."""
    buffer = ed.buffer
    line = buffer.line_of(ed.view.cursor.pos)
    condition: str | None = None
    if prefix is not None:
        condition = await ed.minibuffer.read("Stop when: ", history="conditions") or None
    _toggle(buffer, line, condition)


@command(name="debug.toggle_breakpoint_at_mouse")
def toggle_breakpoint_at_mouse() -> None:
    """Set or remove a breakpoint on the line whose gutter was clicked."""
    frame = ed.frame
    event: Any = frame.last_event if frame is not None else None
    if not isinstance(event, dict) or frame is None:
        return
    view = frame.views.get(event.get("view", -1))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if view is not None:
        _toggle(view.buffer, int(event.get("line", 1)), None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _toggle(buffer: Buffer, line: int, condition: str | None) -> None:
    debugger = _debugger()
    if not debugger.available:
        raise NeomacsError("Breakpoints are unavailable: another debugger is attached")
    if breakpoints.toggle(debugger, buffer, line, condition):
        ed.message(breakpoints.describe(debugger, buffer, line))
    else:
        ed.message(f"Breakpoint removed from line {line}")


@command(name="debug.inspect")
def inspect_error() -> None:
    """Look around the frames of the last error: the stack and variables as
    they were when it was raised.  Evaluation works in them; stepping does not."""
    debugger = _debugger()
    if debugger.paused:
        raise NeomacsError("A command is paused; continue or abort it first")
    exc = ed.last_error
    if exc is None or not debugger.inspect(exc):
        raise NeomacsError("There is no error to inspect")
    show_frame(debugger)
