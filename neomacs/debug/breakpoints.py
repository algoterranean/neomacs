"""Breakpoints (ENGINE 10.3): spans in the source buffer, resolved to code objects."""

from __future__ import annotations

import ast
import gc
from types import CodeType, FunctionType
from typing import TYPE_CHECKING

from neomacs.live import source

if TYPE_CHECKING:
    from neomacs.debug.pause import Debugger
    from neomacs.text.buffer import Buffer
    from neomacs.text.spans import Span

LAYER = "debug"


def _span_on(buffer: Buffer, line: int) -> Span | None:
    start, end = buffer.line_start(line), buffer.line_end(line)
    for span in buffer.layer(LAYER).overlapping(start, max(end, start + 1)):
        if span.get("breakpoint") and buffer.line_of(span.start) == line:
            return span
    return None


def toggle(debugger: Debugger, buffer: Buffer, line: int, condition: str | None = None) -> bool:
    """Set or remove the breakpoint on LINE of BUFFER.  True if one is now set."""
    span = _span_on(buffer, line)
    if span is not None:
        span.remove()
    else:
        columns = list(buffer.options.gutter_columns)
        if "signs" not in columns:
            buffer.options.gutter_columns = ["signs", *columns]
        buffer.layer(LAYER).add(
            buffer.line_start(line),
            buffer.line_end(line),
            breakpoint=True,
            condition=condition,
            gutter={"column": "signs", "text": "●", "face": "breakpoint"},
        )
    refresh(debugger)
    return span is None


def lines_in(buffer: Buffer) -> dict[int, str | None]:
    found: dict[int, str | None] = {}
    for span in buffer.layer(LAYER):
        if span.get("breakpoint"):
            found[buffer.line_of(span.start)] = span.get("condition")
    return found


def any_in(debugger: Debugger, filename: str) -> bool:
    for buffer in debugger.editor.buffers:
        if source.filename_for(buffer) == filename and lines_in(buffer):
            return True
    return filename in debugger.breaks


def _nested(code: CodeType, into: set[CodeType]) -> None:
    if code in into:
        return
    into.add(code)
    for const in code.co_consts:
        if isinstance(const, CodeType):
            _nested(const, into)


def _code_lines(code: CodeType) -> set[int]:
    return {line for _, _, line in code.co_lines() if line is not None}


def codes_for(filenames: set[str]) -> dict[str, set[CodeType]]:
    """Every code object alive that was compiled from one of FILENAMES."""
    found: dict[str, set[CodeType]] = {name: set() for name in filenames}
    for obj in gc.get_objects():
        if isinstance(obj, FunctionType):
            code = obj.__code__
            if code.co_filename in found:
                _nested(code, found[code.co_filename])
    return found


def _statement_line(text: str, line: int) -> int:
    """The line a breakpoint asked for on LINE will stop at: the start of the
    statement LINE is part of, or of the next statement after it."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return line
    best: int | None = None
    following: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        simple = not hasattr(node, "body")
        if simple and node.lineno <= line <= (node.end_lineno or node.lineno):
            best = node.lineno
        elif node.lineno >= line and (following is None or node.lineno < following):
            following = node.lineno
    if best is not None:
        return best
    return following if following is not None else line


def refresh(debugger: Debugger, extra: CodeType | None = None) -> None:
    """Rebuild the breakpoint table from the spans, and apply it."""
    table: dict[str, dict[int, str | None]] = {}
    for buffer in debugger.editor.buffers:
        lines = lines_in(buffer)
        if lines:
            text = buffer.text
            entry = table.setdefault(source.filename_for(buffer), {})
            for line, condition in lines.items():
                entry[_statement_line(text, line)] = condition
    codes = codes_for(set(table))
    if extra is not None and extra.co_filename in codes:
        # Code that is about to run once, as a module is evaluated: no
        # function holds it, so it is not found by looking at the heap.
        _nested(extra, codes[extra.co_filename])
    resolved: dict[str, set[CodeType]] = {}
    for filename in table:
        by_code = {code: _code_lines(code) for code in codes[filename]}
        resolved[filename] = {
            code for code, have in by_code.items() if not have.isdisjoint(table[filename])
        }
    debugger.breaks = table
    debugger.resolved = resolved
    debugger.apply()


def codes_with_breaks(debugger: Debugger) -> set[CodeType]:
    resolved = debugger.resolved
    return set[CodeType]().union(*resolved.values()) if resolved else set()


def describe(debugger: Debugger, buffer: Buffer, line: int) -> str:
    """What to tell the user about the breakpoint just set on LINE."""
    filename = source.filename_for(buffer)
    actual = _statement_line(buffer.text, line)
    where = f"Breakpoint at line {line}"
    if actual != line:
        where += f"; it will stop at line {actual}, where the statement starts"
    loaded = any(actual in _code_lines(code) for code in debugger.resolved.get(filename, ()))
    return where if loaded else where + "; it takes effect when this code is evaluated"
