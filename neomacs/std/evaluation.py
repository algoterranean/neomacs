"""Standard commands: evaluating Python in the running editor (ENGINE 9.5)."""

from __future__ import annotations

import codeop
import weakref
from typing import Annotated, Any

from neomacs.commands import Ask, command
from neomacs.editor import ed
from neomacs.errors import NeomacsError, Quit
from neomacs.keymap import Keymap
from neomacs.live import evaluate, keep, source
from neomacs.live import run as live
from neomacs.modes import Mode

PROMPT = ">>> "


async def _evaluate(start: int, end: int, *, whole: bool = False, show_none: bool = True) -> bool:
    """Evaluate part of the current buffer and show the value or the error."""
    try:
        result = await live.eval_range(ed.buffer, start, end, whole=whole)
    except Quit:
        raise
    except Exception as exc:
        live.report_error(exc)
        return False
    live.show_value(result, show_none=show_none)
    return True


@command
async def eval_buffer() -> None:
    """Evaluate the whole buffer.  Registrations it used to make and no longer
    makes (commands, key bindings, hooks, options) are removed."""
    buffer = ed.buffer
    whole = source.module_for(buffer, ed.scratch) is not ed.scratch
    await _evaluate(0, len(buffer), whole=whole, show_none=False)


@command
async def eval_selection() -> None:
    """Evaluate the selected text.  With several selections, each in buffer order."""
    ranges = [(s.start, s.end) for s in ed.view.selections if not s.empty]
    if not ranges:
        raise NeomacsError("Nothing is selected")
    for start, end in ranges:
        if not await _evaluate(start, end):
            break


@command
async def eval_definition() -> None:
    """Evaluate the top-level statement the cursor is in: a function, a class, an assignment."""
    buffer = ed.buffer
    found = source.definition_at(buffer.text, buffer.line_of(ed.view.cursor.pos))
    if found is None:
        raise NeomacsError("No statement here")
    await _evaluate(buffer.line_start(found[0]), buffer.line_end(found[1]))


@command
async def eval_last_expression() -> None:
    """Evaluate the expression that ends at the cursor."""
    found = source.expression_before(ed.buffer.text, ed.view.cursor.pos)
    if found is None:
        raise NeomacsError("No expression ends here")
    await _evaluate(*found)


@command
async def eval_expression(
    text: Annotated[str, Ask.string("Eval: ", history="eval")],
) -> None:
    """Evaluate Python typed in the minibuffer: in the scratch module, or while
    the debugger is paused, in the selected stack frame."""
    try:
        result = await live.eval_text(text)
    except Quit:
        raise
    except Exception as exc:
        live.report_error(exc)
        return
    live.show_value(result)


@command
def show_traceback() -> None:
    """Show the traceback of the last error from evaluating code."""
    buffer = ed.buffers.get("*traceback*")
    if buffer is None:
        raise NeomacsError("No traceback yet")
    ed.show(buffer, "traceback", focus=True)


@command
def follow_link() -> None:
    """Go to what the text under the cursor refers to."""
    view = ed.view
    for layer in view.buffer.layers:
        for span in layer.at(view.cursor.pos):
            jump: Any = span.get("jump")
            if jump is not None:
                live.open_location(str(jump[0]), int(jump[1]))
                return
            run: Any = span.get("run")
            if callable(run):
                run()
                return
    raise NeomacsError("Nothing to follow here")


@command
def quit_window() -> None:
    """Leave this buffer and go back to the one shown before it."""
    panel: Any = getattr(ed.view, "panel", None)
    if panel is not None:
        panel.close(ed.view)  # a tab of a panel: close it and go back to the text
        return
    window = ed.window
    current = window.buffer
    previous = ed.buffers.get(current.locals.get("_shown_over", "")) if current.locals else None
    if previous is None or previous is current:
        previous = next(
            (b for b in reversed(list(ed.buffers)) if b is not current and b.name[:1] != "*"),
            ed.buffers.get("*scratch*"),
        )
    if previous is not None:
        ed.show(previous, focus=True)


# -- the REPL -------------------------------------------------------------------


class ReplMode(Mode):
    """A buffer in which each input is evaluated in the scratch module."""

    name = "repl"
    interactive = False
    keymap = Keymap("repl")


assert ReplMode.keymap is not None
ReplMode.keymap.bind("<return>", "repl.submit")


def _input_starts() -> weakref.WeakKeyDictionary[Any, Any]:
    # Where the input being typed begins, per REPL buffer.  Kept, so that
    # re-evaluating this module does not lose it.
    return keep("repl.input_starts", weakref.WeakKeyDictionary[Any, Any])


_compiler = codeop.CommandCompiler()
_compiler.compiler.flags |= 0x2000  # ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


@command
def repl() -> None:
    """Show the REPL buffer, creating it if need be."""
    buffer = ed.buffers.get("*repl*")
    if buffer is None:
        buffer = ed.buffers.new("*repl*", PROMPT)
        buffer.set_mode(ReplMode)
        _input_starts()[buffer] = buffer.marker(len(buffer))
    view = ed.show(buffer, "repl", focus=True)
    view.selections.set([(len(buffer), len(buffer))])


@command(name="repl.submit")
async def repl_submit() -> None:
    """Evaluate the input, or start a new line if it is not complete yet."""
    buffer = ed.buffer
    start = _input_starts().get(buffer)
    view = ed.view
    if start is None or view.cursor.pos < start.pos:
        view.selections.primary.insert("\n")
        return
    text = buffer[start.pos : len(buffer)]
    try:
        complete = _compiler(text, "<repl>", "exec") is not None
    except SyntaxError, ValueError, OverflowError:
        complete = True  # let the evaluation report it
    if not complete or (("\n" in text.strip()) and not text.endswith("\n")):
        buffer.insert(len(buffer), "\n")
        view.selections.set([(len(buffer), len(buffer))])
        return
    first_line = buffer.line_of(start.pos)
    buffer.insert(len(buffer), "\n" if not text.endswith("\n") else "")
    output = ""
    try:
        result = await evaluate(
            text.strip("\n"),
            ed.scratch.__dict__,
            filename=source.filename_for(buffer),
            first_line=first_line,
            source_text=None,
        )
        live.announce(ed.scratch.__name__, result)
        if result.value is not live.NO_VALUE and result.value is not None:
            output = repr(result.value) + "\n"
    except Quit:
        raise
    except Exception as exc:
        live.report_error(exc)
        output = f"{type(exc).__name__}: {exc}\n"
    buffer.insert(len(buffer), output + PROMPT)
    start.pos = len(buffer)
    buffer.undo.clear()
    view.selections.set([(len(buffer), len(buffer))])
    view.reveal()
