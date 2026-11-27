"""Where code came from: file names for buffers, `linecache`, the module a
buffer belongs to, and finding the piece of text to evaluate (ENGINE 9.1, 9.2, 9.5)."""

from __future__ import annotations

import ast
import linecache
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

PREFIX = "<neomacs:"
_registered: list[str] = []
MAX_ANONYMOUS = 400  # evaluations kept in linecache under made-up names


def filename_for(buffer: Buffer) -> str:
    """The name code evaluated from BUFFER is compiled under."""
    return str(buffer.file) if buffer.file is not None else f"{PREFIX}{buffer.name}>"


def buffer_name(filename: str) -> str | None:
    """The buffer a made-up file name stands for, if it is one."""
    if filename.startswith(PREFIX) and filename.endswith(">"):
        return filename[len(PREFIX) : -1]
    return None


def register(filename: str, text: str, first_line: int = 1) -> None:
    """Make TEXT what tracebacks and `inspect` see as FILENAME's source.

    The entry has no modification time, so `linecache` never checks it
    against the disk: it shows the code that is actually running.
    """
    lines = [line + "\n" for line in text.split("\n")]
    if first_line > 1:
        existing = linecache.cache.get(filename)
        old = list(existing[2]) if existing is not None and len(existing) == 4 else []
        old.extend("\n" for _ in range(first_line - 1 - len(old)))
        lines = old[: first_line - 1] + lines + old[first_line - 1 + len(lines) :]
    linecache.cache[filename] = (sum(len(line) for line in lines), None, lines, filename)
    if filename.startswith("<") and filename not in _registered:
        _registered.append(filename)
        while len(_registered) > MAX_ANONYMOUS:
            stale = _registered.pop(0)
            if buffer_name(stale) is None:
                linecache.cache.pop(stale, None)


def module_name_for(path: Path) -> str | None:
    """The dotted name PATH would be imported under, if it is in an importable package."""
    path = path.resolve()
    if path.suffix != ".py":
        return None
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and Path(file).resolve() == path:
            return name
    parts = [] if path.stem == "__init__" else [path.stem]
    directory = path.parent
    while (directory / "__init__.py").exists():
        parts.insert(0, directory.name)
        directory = directory.parent
    if len(parts) < 2 and not (path.parent / "__init__.py").exists():
        return None  # a lone file, not part of a package
    roots = {Path(p or ".").resolve() for p in sys.path}
    return ".".join(parts) if directory.resolve() in roots else None


def module_for(buffer: Buffer, scratch: types.ModuleType) -> types.ModuleType:
    """The module BUFFER's code is evaluated in (ENGINE 9.2)."""
    if buffer.file is None:
        return scratch
    name = module_name_for(buffer.file)
    if name is None:
        return scratch
    module = sys.modules.get(name)
    if module is None:
        # Not loaded yet: an empty module, for the buffer to be evaluated into.
        module = types.ModuleType(name)
        module.__file__ = str(buffer.file)
        module.__package__ = name if buffer.file.stem == "__init__" else name.rpartition(".")[0]
        sys.modules[name] = module
    return module


# -- finding what to evaluate ---------------------------------------------------


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _char_pos(lines: list[str], starts: list[int], lineno: int, byte_col: int) -> int:
    line = lines[lineno - 1]
    col = (
        byte_col
        if line.isascii()
        else len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))
    )
    return starts[lineno - 1] + col


def expression_before(text: str, pos: int) -> tuple[int, int] | None:
    """The largest expression that ends exactly at POS, as (start, end) positions."""
    while pos > 0 and text[pos - 1] in " \t":
        pos -= 1
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        lines = text.split("\n")
        starts = _line_starts(text)
        best: tuple[int, int] | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.expr) or node.end_lineno is None:
                continue
            end = _char_pos(lines, starts, node.end_lineno, node.end_col_offset or 0)
            if end != pos:
                continue
            start = _char_pos(lines, starts, node.lineno, node.col_offset)
            if best is None or start < best[0]:
                best = (start, end)
        if best is not None:
            return best
    # The buffer does not parse as it stands: look for an expression on this line.
    line_start = text.rfind("\n", 0, pos) + 1
    for start in range(line_start, pos):
        candidate = text[start:pos]
        if candidate[:1].isspace():
            continue
        try:
            ast.parse(candidate, mode="eval")
        except SyntaxError:
            continue
        return start, pos
    return None


def definition_at(text: str, line: int) -> tuple[int, int] | None:
    """The top-level statement containing 1-based LINE, as (first line, last line).

    Decorators are part of it.  A line inside a class gives the whole class.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
            if first <= line <= (node.end_lineno or node.lineno):
                return first, node.end_lineno or node.lineno
        return None
    # Unparseable: go by indentation.  A statement starts at an unindented line.
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return None

    def starts_block(n: int) -> bool:
        content = lines[n - 1]
        return bool(content.strip()) and not content[0].isspace() and content[0] not in ")]}#"

    first = line
    while first > 1 and not starts_block(first):
        first -= 1
    while first > 1 and lines[first - 2].startswith("@"):
        first -= 1
    last = first
    while last < len(lines) and not starts_block(last + 1):
        last += 1
    while last > first and not lines[last - 1].strip():
        last -= 1
    return (first, last) if lines[first - 1].strip() else None
