"""Standard commands that know the text's structure (ENGINE 5.9): expressions,
brackets, comments, definitions, indentation, and typing pairs."""

from __future__ import annotations

from typing import Annotated, Any

from neomacs.commands import Prefix, command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.hooks import post_command
from neomacs.modes import Mode
from neomacs.options import Option
from neomacs.selection import Selection
from neomacs.text.buffer import Buffer

Option("auto_indent", bool, False, "Indent a new line to suit the one before it.")
Option(
    "auto_pair", bool, True, "In source code, typing an opening bracket or quote adds its partner."
)
Option(
    "show_matching_bracket", bool, True, "Highlight the bracket that matches the one at the cursor."
)

MATCH_LAYER = "bracket-match"


# -- expressions -----------------------------------------------------------------------


@command
def forward_expression(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move over the next N expressions: a name, a string, or a bracketed group."""
    if n < 0:
        backward_expression(sel, -n)
        return
    syntax = sel.buffer.syntax
    pos = sel.head.pos
    for _ in range(n):
        found = syntax.expression_at(pos, 1)
        if found is None:
            break
        pos = found[1]
    sel.move_to(pos)


@command
def backward_expression(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Move back over the previous N expressions."""
    if n < 0:
        forward_expression(sel, -n)
        return
    syntax = sel.buffer.syntax
    pos = sel.head.pos
    for _ in range(n):
        found = syntax.expression_at(pos, -1)
        if found is None:
            break
        pos = found[0]
    sel.move_to(pos)


@command
def up_expression(sel: Selection) -> None:
    """Move out to the opening bracket around the cursor."""
    found = sel.buffer.syntax.scanner.enclosing_brackets(sel.head.pos, sel.head.pos)
    if found is None:
        raise NeomacsError("Not inside brackets")
    sel.move_to(found[0])


@command
def goto_matching_bracket(sel: Selection) -> None:
    """Jump to the bracket that matches the one at, or just before, the cursor."""
    syntax = sel.buffer.syntax
    pos = sel.head.pos
    for at in (pos, pos - 1):
        other = syntax.matching_bracket(at) if at >= 0 else None
        if other is not None:
            # Land just inside a closing bracket's partner, on an opening one's.
            sel.move_to(other + 1 if other > at else other)
            return
    raise NeomacsError("No bracket here, or it has no partner")


@command
def expand_selection() -> None:
    """Grow each selection to the next larger unit around it: the word, the
    brackets, the line, the paragraph, and with a grammar, the syntax node."""
    view = ed.view
    buffer = view.buffer
    syntax = buffer.syntax
    before = view.selections.ranges()
    history: list[Any] = view.buffer.locals.setdefault("_expansions", {}).setdefault(view.id, [])
    if not history or history[-1][1] != before:
        history.clear()
    grown: list[tuple[int, int]] = []
    for sel in view.selections:
        start, end = sel.start, sel.end
        found: tuple[int, int] | None = None
        if start == end:
            found = syntax.word_at(start)
        if found is None or found == (start, end):
            found = syntax.enclosing(start, end)
        grown.append(found if found is not None else (start, end))
    if grown == [(min(a, h), max(a, h)) for a, h in before]:
        raise NeomacsError("Nothing larger to select")
    view.selections.set(grown, view.selections.primary_index)
    view.selections.normalize()
    history.append((before, view.selections.ranges()))


@command
def shrink_selection() -> None:
    """Undo the last `expand_selection`."""
    view = ed.view
    history: list[Any] = view.buffer.locals.get("_expansions", {}).get(view.id, [])
    if not history or history[-1][1] != view.selections.ranges():
        history.clear()
        raise NeomacsError("Nothing to shrink back to")
    before, _ = history.pop()
    view.selections.set(before)


# -- comments -----------------------------------------------------------------------------


@command
def toggle_comment() -> None:
    """Comment the lines of each selection, or uncomment them if they all are."""
    view = ed.view
    buffer = view.buffer
    table = buffer.syntax.table
    marker = table.line_comment
    if marker is None:
        raise NeomacsError("This kind of text has no line comments")
    done: set[int] = set()
    blocks: list[tuple[int, int]] = []
    for sel in view.selections:
        first, last = buffer.line_of(sel.start), buffer.line_of(sel.end)
        if last > first and sel.end == buffer.line_start(last):
            last -= 1  # a selection ending at the start of a line does not include it
        lines = [n for n in range(first, last + 1) if n not in done]
        done.update(lines)
        if lines:
            blocks.append((lines[0], lines[-1]))
    pad = table.comment_padding
    for first, last in reversed(blocks):
        numbers = [n for n in range(first, last + 1) if buffer.line_text(n).strip()]
        if not numbers:
            numbers = [first]
        texts = {n: buffer.line_text(n) for n in numbers}
        commented = all(t.lstrip().startswith(marker) for t in texts.values() if t.strip())
        if commented and any(t.strip() for t in texts.values()):
            for n in reversed(numbers):
                text = texts[n]
                at = len(text) - len(text.lstrip())
                length = len(marker) + (len(pad) if text[at + len(marker) :].startswith(pad) else 0)
                start = buffer.line_start(n) + at
                buffer.delete(start, start + length)
        else:
            indent = min(len(t) - len(t.lstrip()) for t in texts.values())
            for n in reversed(numbers):
                buffer.insert(buffer.line_start(n) + indent, marker + pad)


# -- definitions --------------------------------------------------------------------------


def _top_level_starts(buffer: Buffer) -> list[int]:
    """Lines that begin a top-level block: unindented, and not a comment or a closer."""
    table = buffer.syntax.table
    closers = {c for _, c in table.brackets}
    starts: list[int] = []
    scanner = buffer.syntax.scanner
    for n in range(1, buffer.line_count + 1):
        text = buffer.line_text(n)
        if not text or text[0].isspace() or text[0] in closers:
            continue
        if table.line_comment and text.startswith(table.line_comment):
            continue
        if scanner.state_at(n)[0] != "code":
            continue
        if text.startswith("@") and starts and starts[-1] == n - 1:
            continue  # more decorators of the same definition
        starts.append(n)
    # A decorated definition starts at its first decorator, not at its `def`.
    return [
        n
        for n in starts
        if not (
            n > 1
            and buffer.line_text(n - 1).startswith("@")
            and not buffer.line_text(n).startswith("@")
        )
    ]


def definition_around(buffer: Buffer, pos: int) -> tuple[int, int] | None:
    """The definition containing POS, as (start, end) positions."""
    for name in ("function", "class"):
        found = buffer.syntax.unit_at(pos, name)
        if found is not None:
            return found
    starts = _top_level_starts(buffer)
    line = buffer.line_of(pos)
    before = [n for n in starts if n <= line]
    if not before:
        return None
    first = before[-1]
    after = [n for n in starts if n > first]
    last = (after[0] - 1) if after else buffer.line_count
    while last > first and not buffer.line_text(last).strip():
        last -= 1
    return buffer.line_start(first), buffer.line_end(last)


@command
def start_of_definition(sel: Selection) -> None:
    """Move to the start of the definition the cursor is in, or of the one before."""
    buffer = sel.buffer
    pos = sel.head.pos
    found = definition_around(buffer, pos)
    if found is not None and found[0] == pos and pos > 0:
        found = definition_around(buffer, buffer.line_start(max(1, buffer.line_of(pos) - 1)))
    if found is None:
        raise NeomacsError("No definition here")
    sel.move_to(found[0])


@command
def end_of_definition(sel: Selection) -> None:
    """Move to the end of the definition the cursor is in, or of the next one."""
    buffer = sel.buffer
    pos = sel.head.pos
    found = definition_around(buffer, pos)
    if found is None or found[1] <= pos:
        starts = [n for n in _top_level_starts(buffer) if buffer.line_start(n) > pos]
        found = definition_around(buffer, buffer.line_start(starts[0])) if starts else None
    if found is None:
        raise NeomacsError("No definition here")
    sel.move_to(found[1])


@command
def select_definition() -> None:
    """Select the definition the cursor is in."""
    view = ed.view
    found = definition_around(view.buffer, view.cursor.pos)
    if found is None:
        raise NeomacsError("No definition here")
    view.selections.set([found])


# -- indentation --------------------------------------------------------------------------


def set_indentation(buffer: Buffer, line: int, width: int) -> None:
    """Make LINE's leading whitespace WIDTH columns wide."""
    text = buffer.line_text(line)
    old = len(text) - len(text.lstrip(" \t"))
    if buffer.options.indent_with_tabs:
        tab = int(buffer.options.tab_width)
        new = "\t" * (width // tab) + " " * (width % tab)
    else:
        new = " " * width
    if text[:old] != new:
        start = buffer.line_start(line)
        buffer.replace(start, start + old, new)


@command
def indent_line(sel: Selection) -> None:
    """Indent the cursor's line as its surroundings suggest.  Repeating the
    command steps the indentation back a level at a time."""
    buffer = sel.buffer
    syntax = buffer.syntax
    if not sel.empty:
        for line in range(
            buffer.line_of(sel.start), buffer.line_of(max(sel.start, sel.end - 1)) + 1
        ):
            wanted = syntax.indent_for(line)
            if wanted is not None and buffer.line_text(line).strip():
                set_indentation(buffer, line, wanted)
        return
    line = buffer.line_of(sel.head.pos)
    wanted = syntax.indent_for(line)
    if wanted is None:
        sel.replace("\t")
        return
    current = syntax.indentation(line)
    frame = ed.frame
    repeated = (
        frame is not None
        and frame.last_command is not None
        and frame.last_command[0] == "indent_line"
    )
    unit = int(buffer.options.tab_width)
    if current == wanted and repeated:
        wanted = wanted - unit if wanted >= unit else syntax.indent_for(line) or 0
    elif repeated and current < wanted:
        wanted = current - unit if current >= unit else wanted
    set_indentation(buffer, line, max(0, wanted))
    text = buffer.line_text(line)
    lead = len(text) - len(text.lstrip(" \t"))
    start = buffer.line_start(line)
    if sel.head.pos < start + lead:
        sel.move_to(start + lead)


@command(amalgamate=True)
def newline_and_indent(sel: Selection) -> None:
    """Insert a line break, and indent the new line if `auto_indent` is on."""
    buffer = sel.buffer
    sel.replace("\n")
    if buffer.options.auto_indent:
        line = buffer.line_of(sel.head.pos)
        wanted = buffer.syntax.indent_for(line)
        if wanted:
            set_indentation(buffer, line, wanted)
            sel.move_to(
                buffer.line_start(line)
                + len(buffer.line_text(line))
                - len(buffer.line_text(line).lstrip(" \t"))
            )


# -- pairs ----------------------------------------------------------------------------------


class AutoPairMode(Mode):
    """Typing an opening bracket or quote also inserts its partner."""

    name = "auto-pair"
    minor = True


def pair_insert(sel: Selection, text: str) -> bool:
    """Handle typed TEXT as half of a pair.  False if it is an ordinary character."""
    buffer = sel.buffer
    if len(text) != 1 or buffer.minor(AutoPairMode) is None:
        return False
    table = buffer.syntax.table
    pairs = {o: c for o, c in table.pairs if len(o) == 1 and len(c) == 1}
    closers = set(pairs.values())
    pos = sel.head.pos
    following = buffer[pos] if pos < len(buffer) else ""
    if not sel.empty and text in pairs:
        inner = sel.text
        start = sel.start
        sel.replace(text + inner + pairs[text])
        sel.select(start + 1, start + 1 + len(inner))  # still selected, now wrapped
        return True
    if text in closers and following == text and sel.empty:
        if text not in pairs or buffer.syntax.kind_at(pos) == "string":
            sel.move_to(pos + 1)  # type over the closer that is already there
            return True
    if text not in pairs or not sel.empty:
        return False
    if following and not following.isspace() and following not in closers:
        return False  # in front of text: just the one character
    if pairs[text] == text:
        previous = buffer[pos - 1] if pos > 0 else ""
        if table.is_word(previous) or buffer.syntax.kind_at(pos) != "code":
            return False  # an apostrophe in a word, or a quote inside a string or comment
    sel.replace(text + pairs[text])
    sel.move_to(pos + 1)
    return True


def pair_delete(sel: Selection) -> bool:
    """Backspace between an empty pair removes both halves."""
    buffer = sel.buffer
    pos = sel.head.pos
    if not sel.empty or buffer.minor(AutoPairMode) is None or pos == 0 or pos >= len(buffer):
        return False
    pairs = {o: c for o, c in buffer.syntax.table.pairs if len(o) == 1 and len(c) == 1}
    if pairs.get(buffer[pos - 1]) != buffer[pos]:
        return False
    buffer.delete(pos - 1, pos + 1)
    return True


# -- the bracket that matches the one at the cursor ------------------------------------------


@post_command.add
def highlight_matching_bracket(_command: Any) -> None:
    frame = ed.frame
    if frame is None:
        return
    view = frame.focus
    buffer = view.buffer
    shown: Any = buffer.locals.get("_bracket_match")
    wanted: tuple[int, int] | None = None
    mode = buffer.mode
    if mode is not None and mode.highlight and buffer.options.show_matching_bracket:
        if len(view.selections) == 1 and view.selections.primary.empty:
            pos = view.cursor.pos
            syntax = buffer.syntax
            for at in (pos, pos - 1):
                other = syntax.matching_bracket(at) if 0 <= at < len(buffer) else None
                if other is not None:
                    wanted = (min(at, other), max(at, other))
                    break
    if shown is not None and shown[0] == wanted and shown[1] == buffer.tick:
        return
    layer = buffer.layer(MATCH_LAYER)
    if len(layer):
        layer.clear()
    buffer.locals["_bracket_match"] = (wanted, buffer.tick)
    if wanted is not None:
        for at in wanted:
            layer.add(at, at + 1, priority=20, face="bracket-match")
