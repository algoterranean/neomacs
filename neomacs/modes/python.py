"""Python mode."""

from __future__ import annotations

import keyword
from dataclasses import replace

from neomacs.keymap import Keymap
from neomacs.modes import ProgMode
from neomacs.text.buffer import Buffer


class PythonMode(ProgMode):
    """Major mode for Python source."""

    name = "python"
    patterns = ["*.py", "*.pyi", "*.pyw", "SConstruct", "SConscript"]
    interpreters = ["python", "python3", "pypy", "pypy3"]
    keymap = Keymap("python")
    defaults = {"tab_width": 4, "indent_with_tabs": False}
    syntax = replace(
        ProgMode.syntax, line_comment="#", quotes=('"""', "'''", '"', "'"), word_chars="_"
    )
    grammar = "python"
    keywords = frozenset(keyword.kwlist) - {"None", "True", "False"} | {"match", "case", "type"}
    constants = frozenset({"None", "True", "False", "Ellipsis", "NotImplemented", "self", "cls"})
    definers = {"def": "function", "class": "type"}

    @classmethod
    def indent_for(cls, buffer: Buffer, line: int) -> int | None:
        syntax = buffer.syntax
        scanner = syntax.scanner
        if scanner.state_at(line)[0] == "string":
            return None  # inside a multi-line string: leave it alone
        previous = line - 1
        while previous >= 1 and (
            not buffer.line_text(previous).strip() or scanner.state_at(previous)[0] != "code"
        ):
            previous -= 1
        if previous < 1:
            return 0
        unit = int(buffer.options.tab_width)
        scan = scanner.line(previous)
        text = buffer.line_text(previous)
        code = text
        for start, _end, kind in scan.segments:
            if kind == "comment":
                code = text[:start]
        code = code.rstrip()
        opens = {o for o, _ in syntax.table.brackets}
        depth = sum(1 if b in opens else -1 for _, b in scan.brackets)
        indent = syntax.indentation(previous)
        if depth < 0:
            # The line before closed a bracket: the statement began where it was opened.
            end = buffer.line_start(previous) + scan.brackets[-1][0]
            opener = syntax.matching_bracket(end)
            if opener is not None:
                indent = syntax.indentation(buffer.line_of(opener))
        if depth > 0 or code.endswith(":"):
            indent += unit
        elif code.split() and code.split()[0] in _LEAVES:
            indent -= unit
        first = buffer.line_text(line).split()
        word = first[0].rstrip(":") if first else ""
        if word in _DEDENTS:
            # It lines up with the `if` or `try` it belongs to: one level out
            # from the block before it, or level with a block that is empty.
            indent = syntax.indentation(previous) - (0 if code.endswith(":") else unit)
        elif first and first[0][0] in ")]}":
            indent -= unit
        return max(0, indent)


_LEAVES = {"return", "pass", "raise", "break", "continue"}
_DEDENTS = {"else", "elif", "except", "finally"}
