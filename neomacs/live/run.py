"""Evaluating text from buffers and files, and showing what came of it."""

from __future__ import annotations

import sys
import textwrap
import traceback
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs.editor import current
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap
from neomacs.live import source
from neomacs.live.evaluate import NO_VALUE, Evaluation, evaluate
from neomacs.modes import Mode

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

VALUE_WIDTH = 240  # characters of a value shown in the message area
_OWN_FILES = (str(Path(__file__).with_name("evaluate.py")), __file__)


class SpecialMode(Mode):
    """The mode of buffers the editor writes and the user reads: help, tracebacks."""

    name = "special"
    keymap = Keymap("special")


class TracebackMode(SpecialMode):
    name = "traceback"
    keymap = Keymap("traceback")


assert SpecialMode.keymap is not None and TracebackMode.keymap is not None
SpecialMode.keymap.bind("q", "quit_window")
SpecialMode.keymap.bind("<return>", "follow_link")
SpecialMode.keymap.bind("<double-mouse-1>", "follow_link")


async def eval_range(
    buffer: Buffer, start: int, end: int, *, whole: bool = False, mode: str = "auto"
) -> Evaluation:
    """Evaluate the text of BUFFER between START and END in the module the
    buffer belongs to (ENGINE 9.2), and warn about anything left stale."""
    ed = current()
    module = source.module_for(buffer, ed.scratch)
    line = buffer.line_of(start)
    text = buffer[start:end]
    lead = buffer[buffer.line_start(line) : start]
    if lead and lead.isspace():
        text = textwrap.dedent(lead + text)  # an indented block, such as one method
    result = await evaluate(
        text,
        module.__dict__,
        filename=source.filename_for(buffer),
        first_line=line,
        mode=mode,
        whole_module=whole,
        source_text=buffer.text,
    )
    announce(module.__name__, result)
    return result


async def load_file(path: Path | str) -> Evaluation:
    """Load a file of Python from disk through the same pipeline as a buffer."""
    ed = current()
    resolved = Path(path).expanduser().resolve()
    text = resolved.read_text(encoding="utf-8")
    name = source.module_name_for(resolved)
    module = ed.scratch
    if name is not None:
        module = sys.modules.get(name) or types.ModuleType(name)
        module.__file__ = str(resolved)
        sys.modules[name] = module
    result = await evaluate(
        text, module.__dict__, filename=str(resolved), mode="exec", whole_module=name is not None
    )
    announce(module.__name__, result)
    return result


def announce(module: str, result: Evaluation) -> None:
    """Tell the user what an evaluation could not update, and what it removed."""
    ed = current()
    for name, reason in result.rebound:
        text = (
            f"{module}.{name} was replaced, not updated in place: {reason}. "
            "Anything still holding the old one keeps the old behaviour."
        )
        ed.eval_warnings.append((module, name, reason))
        ed.message(text, kind="warning")
    for kind, key in result.removed:
        shown = key if isinstance(key, str) else key[-1]
        ed.message(f"Removed {kind} {shown!r}: {module} no longer defines it", kind="log")


def show_value(result: Evaluation, *, show_none: bool = True) -> None:
    ed = current()
    if result.value is NO_VALUE or (result.value is None and not show_none):
        ed.message("Evaluated")
        return
    text = repr(result.value)
    if len(text) > VALUE_WIDTH or "\n" in text:
        ed.message(text, kind="log")  # the whole of it, in *messages*
        text = text.split("\n", 1)[0][:VALUE_WIDTH] + "…"
    ed.message(text)


# -- tracebacks -----------------------------------------------------------------


def _frames(exc: BaseException) -> list[traceback.FrameSummary]:
    frames = list(traceback.extract_tb(exc.__traceback__))
    # Drop the editor's own frames above the code that was evaluated.
    cut = max((i + 1 for i, f in enumerate(frames) if f.filename in _OWN_FILES), default=0)
    return frames[cut:]


def _write(buffer: Buffer, exc: BaseException, seen: set[int]) -> None:
    seen.add(id(exc))
    cause, context = exc.__cause__, exc.__context__
    if cause is not None and id(cause) not in seen:
        _write(buffer, cause, seen)
        buffer.insert(len(buffer), "\nThe above was the direct cause of the following:\n\n")
    elif context is not None and not exc.__suppress_context__ and id(context) not in seen:
        _write(buffer, context, seen)
        buffer.insert(len(buffer), "\nWhile handling the above, another exception occurred:\n\n")
    layer = buffer.layer("links")
    frames = _frames(exc)
    if frames:
        buffer.insert(len(buffer), "Traceback (most recent call last):\n")
    for frame in frames:
        start = len(buffer)
        buffer.insert(start, f"  {frame.filename}, line {frame.lineno}, in {frame.name}\n")
        if frame.line:
            buffer.insert(len(buffer), f"    {frame.line.strip()}\n")
        layer.add(start + 2, len(buffer) - 1, face="underline", jump=(frame.filename, frame.lineno))
    if isinstance(exc, SyntaxError) and exc.filename and exc.lineno:
        start = len(buffer)
        buffer.insert(start, f"  {exc.filename}, line {exc.lineno}\n")
        if exc.text:
            buffer.insert(len(buffer), f"    {exc.text.strip()}\n")
        layer.add(start + 2, len(buffer) - 1, face="underline", jump=(exc.filename, exc.lineno))
    start = len(buffer)
    summary = "".join(traceback.format_exception_only(exc)).rstrip("\n")
    buffer.insert(start, summary.split("\n")[-1] + "\n")
    buffer.layer("faces").add(start, len(buffer) - 1, face="error")


def special_buffer(name: str, mode: type[Mode]) -> Buffer:
    """The buffer called NAME, emptied and ready to be written to."""
    ed = current()
    buffer = ed.buffers.get(name) or ed.buffers.new(name)
    buffer.read_only = False
    for layer in buffer.layers:
        layer.clear()
    buffer.replace(0, len(buffer), "")
    if not isinstance(buffer.mode, mode):
        buffer.mode = mode()
    return buffer


def finish_special(buffer: Buffer) -> None:
    buffer.undo.clear()
    buffer.saved_tick = buffer.tick
    buffer.read_only = True


def report_error(exc: BaseException) -> None:
    """Record EXC in the `*traceback*` buffer and tell the user."""
    ed = current()
    buffer = special_buffer("*traceback*", TracebackMode)
    _write(buffer, exc, set())
    finish_special(buffer)
    summary = "".join(traceback.format_exception_only(exc)).strip().split("\n")[-1]
    ed.message(buffer.text, kind="log")
    ed.message(f"{summary}  (show_traceback for the rest)", kind="error")


def open_location(filename: str, line: int) -> None:
    """Show LINE of the file, or of the unsaved buffer, that FILENAME names."""
    ed = current()
    name = source.buffer_name(filename)
    target: Any = ed.buffers.get(name) if name is not None else None
    if target is None and name is None and Path(filename).exists():
        target = ed.buffers.visit(filename)
    if target is None:
        raise NeomacsError(f"Cannot find {filename}")
    window = ed.window
    window.jumps.push(window.buffer, window.cursor.pos)
    view = ed.show(target, focus=True)
    pos = target.line_start(max(1, min(target.line_count, line)))
    view.selections.set([(pos, pos)])
    view.reveal(pos, "center")
