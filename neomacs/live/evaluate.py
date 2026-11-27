"""Evaluating source text in a namespace.

This is the minimal form used by `engine.eval`.  The full pipeline (line
offsets, top-level await, in-place patching, linecache) is ENGINE 9.
"""

from __future__ import annotations

import ast
from typing import Any, Final


class _NoValue:
    def __repr__(self) -> str:
        return "NO_VALUE"


NO_VALUE: Final = _NoValue()


def evaluate(
    source: str,
    namespace: dict[str, Any],
    *,
    filename: str = "<eval>",
    mode: str = "auto",
) -> Any:
    """Execute SOURCE in NAMESPACE.

    With MODE "auto", if the last top-level statement is an expression its
    value is returned; otherwise NO_VALUE.  MODE "exec" never returns a value.
    """
    tree = ast.parse(source, filename, "exec")
    last: ast.Expression | None = None
    if mode != "exec" and tree.body and isinstance(tree.body[-1], ast.Expr):
        expr = tree.body.pop()
        assert isinstance(expr, ast.Expr)
        last = ast.Expression(expr.value)
        ast.copy_location(last, expr)
    if tree.body:
        exec(compile(tree, filename, "exec"), namespace)
    if last is None:
        return NO_VALUE
    return eval(compile(last, filename, "eval"), namespace)
