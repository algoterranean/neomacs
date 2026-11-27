"""Standard commands: having unsaved text back after the engine died (ENGINE 11.1)."""

from __future__ import annotations

import time
from typing import Any

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.journal import Entry, Journal
from neomacs.keymap import Keymap
from neomacs.live import run as live
from neomacs.text.buffer import Buffer

LIST = "*recover*"


class RecoverMode(live.SpecialMode):
    """The mode of the list of unsaved text that can be had back."""

    name = "recover"
    interactive = False
    keymap = Keymap("recover")


assert RecoverMode.keymap is not None
RecoverMode.keymap.bind("<return>", "recover.take")
RecoverMode.keymap.bind("<double-mouse-1>", "recover.take")
RecoverMode.keymap.bind("d", "recover.discard")


def _journal() -> Journal:
    journal: Journal | None = ed.journal
    if journal is None:
        raise NeomacsError("This engine keeps no recovery journal")
    return journal


def _write(entries: list[Entry]) -> Buffer:
    buffer = live.special_buffer(LIST, RecoverMode)
    if not entries:
        buffer.insert(0, "Nothing to recover.\n")
    else:
        buffer.insert(
            0,
            "Unsaved text kept when the editor last stopped.\n"
            "Return on a line opens that text in a buffer, unsaved; d discards it.\n\n",
        )
    layer = buffer.layer("links")
    for entry in entries:
        start = len(buffer)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.time))
        where = str(entry.file) if entry.file is not None else "(no file)"
        note = "  the file has changed since" if entry.file_changed else ""
        buffer.insert(start, f"{entry.name}  {where}  {when}  {len(entry.text)} characters{note}\n")
        layer.add(start, len(buffer) - 1, face="underline", entry=entry)
    live.finish_special(buffer)
    return buffer


def _entry_here() -> Entry:
    view = ed.view
    pos = view.cursor.pos
    line_start = view.buffer.line_start(view.buffer.line_of(pos))
    for span in view.buffer.layer("links").at(line_start):
        entry: Any = span.get("entry")
        if isinstance(entry, Entry):
            return entry
    raise NeomacsError("No unsaved text on this line")


def _refresh() -> None:
    listing = ed.buffers.get(LIST)
    if listing is not None:
        _write(_journal().left_behind())


@command
def recover() -> None:
    """List the unsaved text that was kept when the editor last stopped."""
    buffer = _write(_journal().left_behind())
    shown = ed.window.buffer
    if shown is not buffer:
        buffer.locals["_shown_over"] = shown.name
    view = ed.show(buffer, "help", focus=True)
    first = buffer.line_start(min(4, buffer.line_count))
    view.selections.set([(first, first)])
    view.reveal(0, "top")


@command(name="recover.take")
def recover_take() -> None:
    """Open the unsaved text on this line in a buffer.  The buffer is left
    unsaved; undoing there gives back what its file has."""
    entry = _entry_here()
    try:
        buffer = _journal().recover(entry)
    except FileNotFoundError as exc:
        _refresh()
        raise NeomacsError(str(exc)) from None
    _refresh()
    ed.show(buffer, focus=True)
    ed.message(f"Recovered {buffer.name}. It is not saved yet.")


@command(name="recover.discard")
def recover_discard() -> None:
    """Throw away the unsaved text on this line."""
    entry = _entry_here()
    _journal().discard(entry)
    _refresh()
    ed.message(f"Discarded the unsaved text of {entry.name}")
