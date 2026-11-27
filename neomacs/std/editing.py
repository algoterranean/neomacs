"""Standard commands: inserting, deleting, killing, yanking, undoing, selecting."""

from __future__ import annotations

from typing import Annotated, Any, cast

from neomacs.clipboard import Entry
from neomacs.commands import KeyText, Prefix, command
from neomacs.editor import ed
from neomacs.selection import Selection


@command(amalgamate=True)
def self_insert(
    sel: Selection, text: Annotated[str, KeyText] = "", n: Annotated[int, Prefix()] = 1
) -> None:
    """Insert the character that was typed."""
    if n == 1:
        from neomacs.std.structure import pair_insert

        if pair_insert(sel, text):
            return
    sel.replace(text * max(1, n))


@command
def insert_text(text: str) -> None:
    """Insert TEXT at every cursor.  Text committed by an input method arrives here."""
    for sel in ed.view.selections:
        sel.replace(text)


@command
def self_insert_tab(sel: Selection) -> None:
    """Insert a tab character."""
    sel.replace("\t")


@command
def newline(sel: Selection) -> None:
    """Insert a line break."""
    sel.replace("\n")


@command
def delete_char(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Delete the selection, or N characters after the cursor."""
    if not sel.empty:
        sel.delete()
    else:
        sel.head.delete_char(n)
        sel.collapse()


def _pair_delete(sel: Selection) -> bool:
    from neomacs.std.structure import pair_delete

    return pair_delete(sel)


@command
def delete_backward_char(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:
    """Delete the selection, or N characters before the cursor."""
    if not sel.empty:
        sel.delete()
    elif n == 1 and _pair_delete(sel):
        pass
    else:
        sel.head.delete_backward_char(n)
        sel.collapse()


@command
def delete_backward_codepoint(sel: Selection) -> None:
    """Delete one code point before the cursor: just the accent of an accented letter."""
    pos = sel.head.pos
    if pos > 0:
        sel.buffer.delete(pos - 1, pos)
        sel.collapse()


@command
def transpose_chars(sel: Selection) -> None:
    """Swap the characters either side of the cursor."""
    buf = sel.buffer
    pos = sel.head.pos
    if pos >= len(buf) or buf[pos] == "\n":
        pos = buf.prev_boundary(pos)
    left = buf.prev_boundary(pos)
    right = buf.next_boundary(pos)
    if left == pos or right == pos:
        return
    buf.replace(left, right, buf[pos:right] + buf[left:pos])
    sel.select(right, right)


def _kill(ranges: list[tuple[Selection, int, int]], *, delete: bool = True) -> None:
    """Put the text of RANGES on the kill ring as one entry, and delete it if asked."""
    pieces = [sel.buffer[start:end] for sel, start, end in ranges]
    ed.kill_ring.push(pieces, ed.frame, source="kill" if delete else "copy")
    if delete:
        # Select everything first: the ranges are positions, and stop being
        # right as soon as the first one is deleted.
        for sel, start, end in ranges:
            sel.select(start, end)
        for sel, _, _ in ranges:
            sel.delete()


@command
def kill_selection() -> None:
    """Cut the selected text to the kill ring and the clipboard."""
    selections = [s for s in ed.view.selections if not s.empty]
    if not selections:
        ed.message("Nothing is selected")
        return
    _kill([(s, s.start, s.end) for s in selections])


@command
def copy_selection() -> None:
    """Copy the selected text to the kill ring and the clipboard."""
    selections = [s for s in ed.view.selections if not s.empty]
    if not selections:
        ed.message("Nothing is selected")
        return
    _kill([(s, s.start, s.end) for s in selections], delete=False)
    ed.view.selections.collapse()


@command
def kill_line() -> None:
    """Kill from each cursor to the end of its line, or the line break if already there."""
    ranges: list[tuple[Selection, int, int]] = []
    for sel in ed.view.selections:
        buf = sel.buffer
        pos = sel.head.pos
        end = buf.line_end(buf.line_of(pos))
        if end == pos and pos < len(buf):
            end = pos + 1
        ranges.append((sel, pos, end))
    _kill(ranges)


@command
def kill_word(n: Annotated[int, Prefix()] = 1) -> None:
    """Kill N words after each cursor."""
    ranges: list[tuple[Selection, int, int]] = []
    for sel in ed.view.selections:
        end = sel.head.pos
        for _ in range(max(1, n)):
            end = sel.buffer.syntax.next_word_end(end)
        ranges.append((sel, sel.head.pos, end))
    _kill(ranges)


@command
def backward_kill_word(n: Annotated[int, Prefix()] = 1) -> None:
    """Kill N words before each cursor."""
    ranges: list[tuple[Selection, int, int]] = []
    for sel in ed.view.selections:
        start = sel.head.pos
        for _ in range(max(1, n)):
            start = sel.buffer.syntax.prev_word_start(start)
        ranges.append((sel, start, sel.head.pos))
    _kill(ranges)


def insert_entry(view: Any, entry: Entry, replace: list[tuple[int, int]] | None = None) -> None:
    """Insert ENTRY at every selection of VIEW, or in place of REPLACE, the
    ranges a yank just made.  What is inserted is remembered, for a
    `yank_from_ring` that follows to replace in its turn."""
    if replace is not None:
        view.selections.set(replace)
    selections = list(view.selections)
    texts = ed.kill_ring.distribute(entry, len(selections))
    for sel, text in zip(selections, texts, strict=True):
        sel.replace(text)
    made = [
        (sel.head.pos - len(text), sel.head.pos)
        for sel, text in zip(selections, texts, strict=True)
    ]
    view.buffer.locals["_last_yank"] = (view.id, view.buffer.tick, made)


def last_yank(view: Any) -> list[tuple[int, int]] | None:
    """The ranges the last yank in VIEW inserted, if nothing has changed the
    buffer since and no other command has run (KILLRING K4)."""
    found: Any = view.buffer.locals.get("_last_yank")
    last: Any = view.frame.last_command
    if found is None or last is None or last[0] not in ("yank", "yank_from_ring"):
        return None
    view_id, tick, made = found
    if view_id != view.id or tick != view.buffer.tick:
        return None
    return list(made)


@command
async def yank() -> None:
    """Insert the most recently killed text, or the system clipboard if it is newer."""
    view = ed.view
    entry = await ed.kill_ring.current(ed.frame)
    if entry is None:
        ed.message("The kill ring is empty")
        return
    insert_entry(view, entry)


@command
def paste(text: str) -> None:
    """Insert TEXT at every cursor.  A paste the frontend started arrives here."""
    for sel in ed.view.selections:
        sel.replace(text)


def _restore(saved: Any, fallback: int) -> None:
    """Put the cursors back as an undo group recorded them: (ranges, primary index)."""
    view = ed.view
    clamp = view.buffer.clamp
    if isinstance(saved, tuple) and saved and saved[0]:
        both = cast(tuple[list[tuple[int, int]], int], saved)
        ranges, primary = both
        view.selections.set([(clamp(a), clamp(h)) for a, h in ranges], primary)
    else:
        view.selections.set([(fallback, fallback)])


@command
def undo() -> None:
    """Undo the last change, putting every cursor back."""
    group = ed.buffer.undo.undo()
    if group is None:
        ed.message("Nothing to undo")
    else:
        _restore(group.before, group.edits[0].pos if group.edits else 0)


@command
def redo() -> None:
    """Redo what was last undone."""
    group = ed.buffer.undo.redo()
    if group is None:
        ed.message("Nothing to redo")
    else:
        last = group.edits[-1] if group.edits else None
        _restore(group.after, last.pos + len(last.inserted) if last else 0)


@command
def set_anchor() -> None:
    """Start selecting: from now on motion moves only the cursor end of each selection."""
    selections = ed.view.selections
    for sel in selections:
        sel.collapse()
    selections.extending = True


@command
def select_all() -> None:
    """Select the whole buffer."""
    selections = ed.view.selections
    selections.keep_primary()
    selections.primary.select(0, len(ed.buffer))


@command
def restore_last_selection() -> None:
    """Bring back the selections as they were before they last collapsed."""
    ed.view.selections.restore_last()


@command
def keyboard_quit() -> None:
    """Cancel: stop selecting, drop extra cursors, forget a pending prefix."""
    selections = ed.view.selections
    if selections.extending or len(selections) == 1:
        selections.collapse()
    else:
        selections.keep_primary()
    selections.extending = False
    ed.message("Quit")
