"""The evaluation pipeline (ENGINE 9.1).

Source text is parsed, executed in a module's namespace, and whatever it
redefines is patched in place.  Evaluating a buffer, a selection or a whole
file from disk all come through `evaluate`.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from types import CodeType, FunctionType
from typing import Any, Final

from neomacs.live import patch, source
from neomacs.ownership import ownership


class _NoValue:
    def __repr__(self) -> str:
        return "NO_VALUE"


NO_VALUE: Final = _NoValue()

_FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

# Called with each code object the pipeline compiles, before it runs, and the
# file name it was compiled under.  The debugger applies breakpoints here.
on_compile: list[Callable[[CodeType, str], None]] = []


def _compiled(code: CodeType, filename: str) -> CodeType:
    for listener in list(on_compile):
        listener(code, filename)
    return code


@dataclass
class Evaluation:
    """What an evaluation did, besides its value."""

    value: Any = NO_VALUE
    # Names whose old objects could not be updated in place, with the reason.
    rebound: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    # Registrations the module made last time and not this time, now removed.
    removed: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])


def _definitions(tree: ast.Module) -> list[str]:
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


async def evaluate(
    text: str,
    namespace: dict[str, Any],
    *,
    filename: str = "<eval>",
    first_line: int = 1,
    mode: str = "auto",
    whole_module: bool = False,
    source_text: str | None = None,
    local_names: Any = None,
) -> Evaluation:
    """Execute TEXT in NAMESPACE.

    FIRST_LINE is where TEXT begins in the file or buffer called FILENAME, so
    that tracebacks point at the right lines; SOURCE_TEXT, if given, is the
    whole of that file or buffer.  With MODE "auto", if the last top-level
    statement is an expression its value is returned.  With WHOLE_MODULE,
    registrations the module made before and does not make now are removed.

    With LOCAL_NAMES, a mapping, the text runs as if inside a function whose
    variables those are: the debugger passes a stack frame's.  Nothing is
    patched then; definitions become variables of that frame.
    """
    tree = ast.parse(text, filename, "exec")
    if first_line > 1:
        ast.increment_lineno(tree, first_line - 1)
    last: ast.Expression | None = None
    if mode != "exec" and tree.body and isinstance(tree.body[-1], ast.Expr):
        expr = tree.body.pop()
        assert isinstance(expr, ast.Expr)
        last = ast.Expression(expr.value)
    module = str(namespace.get("__name__", "?"))
    names = _definitions(tree)
    before = {name: namespace.get(name) for name in names}
    # Register the text before running it, so an error is shown against it.
    source.register(filename, source_text if source_text is not None else text, first_line
                    if source_text is None else 1)  # fmt: skip
    result = Evaluation()
    with ownership.tracking(module) as made:
        if tree.body:
            code = _compiled(compile(tree, filename, "exec", flags=_FLAGS), filename)
            pending = eval(code, namespace, local_names)
            if code.co_flags & inspect.CO_COROUTINE:
                await pending
        for name in names if local_names is None else []:
            old, new = before[name], namespace.get(name)
            keep, reason = patch.redefine(module, name, old, new)
            if keep is not new:
                namespace[name] = keep
            if reason is not None and isinstance(old, (FunctionType, type)):
                result.rebound.append((name, reason))
        if last is not None:
            code = _compiled(compile(last, filename, "eval", flags=_FLAGS), filename)
            value = eval(code, namespace, local_names)
            if code.co_flags & inspect.CO_COROUTINE:
                value = await value
            result.value = value
    if whole_module:
        result.removed = ownership.release(module, keep=made)
    return result
