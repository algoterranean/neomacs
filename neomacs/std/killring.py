"""Standard commands: choosing from the kill ring (KILLRING.md section 7)."""

from __future__ import annotations

from typing import Any

from neomacs.clipboard import Entry
from neomacs.commands import command
from neomacs.completion import Candidate
from neomacs.editor import ed
from neomacs.errors import NeomacsError, Quit
from neomacs.keymap import Keymap
from neomacs.minibuffer import Minibuffer, MinibufferMode
from neomacs.panels import InPanel
from neomacs.std.editing import insert_entry, last_yank
from neomacs.text.buffer import Buffer
from neomacs.window import View

PLACE = "chooser"  # the part of a frontend's window the chooser goes in, if it has one
PREVIEW = "*kill ring*"
ROW = 72  # characters of an entry shown in its row of the list
RULE = "─" * 24  # between the pieces of an entry made with several cursors


class KillRingMode(MinibufferMode):
    """The mode of the chooser's input.  What is typed narrows the list."""

    name = "kill-ring"
    interactive = False
    keymap = Keymap("kill-ring")


assert KillRingMode.keymap is not None
for _key, _name in [
    ("M-y", "minibuffer.next"),  # tapping the key that opened it walks the ring (K8)
    ("C-d", "kill_ring.remove"),
    ("C-v", "kill_ring.preview_down"),
    ("M-v", "kill_ring.preview_up"),
    ("<next>", "kill_ring.preview_down"),
    ("<prior>", "kill_ring.preview_up"),
]:
    KillRingMode.keymap.bind(_key, _name)


def _folds(typed: str) -> bool:
    """Case is ignored unless a capital was typed, as in search."""
    return typed == typed.lower()


def _row(entry: Entry, typed: str) -> Candidate:
    """ENTRY as one row of the list: the line TYPED is found on, or the first
    that is not blank, and a note of how much more there is."""
    text = entry.text
    lines = text.split("\n")
    shown = next((line for line in lines if line.strip()), "")
    if typed:
        fold = _folds(typed)
        needle = typed
        for line in lines:
            if needle in (line.lower() if fold else line):
                shown = line
                break
    shown = shown.strip().replace("\t", " ")
    if not shown:
        shown = "⏎" * min(len(lines) - 1, 8) or "(blank)"
    if len(shown) > ROW:
        shown = shown[: ROW - 1] + "…"
    if len(entry.pieces) > 1:
        note = f"{len(entry.pieces)} pieces"
    else:
        count = len(lines) - (1 if text.endswith("\n") and len(lines) > 1 else 0)
        note = f"{count} lines" if count > 1 else ""
    return Candidate(shown, note, entry)


class _Ring:
    """The kill ring as candidates, newest first, narrowed by what was typed."""

    async def candidates(self, text: str, pos: int) -> list[Candidate]:
        fold = _folds(text)
        found: list[Candidate] = []
        for entry in reversed(ed.kill_ring.entries):
            whole = entry.text.lower() if fold else entry.text
            if text in whole:
                found.append(_row(entry, text))
        return found


class Chooser:
    """One use of the chooser: its input, and the buffer the preview is written in."""

    def __init__(self, box: Minibuffer, origin: Buffer) -> None:
        self.box = box
        frame = box.frame
        # A buffer of its own: two choosers open in two frames each show what
        # their own highlight is on.  It goes when the chooser does.
        self.buffer = ed.buffers.new(PREVIEW)
        self.buffer.locals["no_journal"] = True
        self.buffer.option_values["gutter_columns"] = []  # text to look at, not lines to count
        # Shown as the text will look where it is going.
        origin_mode: Any = origin.mode
        if origin_mode is not None and type(self.buffer.mode) is not type(origin_mode):
            self.buffer.set_mode(type(origin_mode))
        self.view: View | None = None
        self._panel: Any = None
        if PLACE in frame.places:
            # The frontend draws it beside the list.
            self.view = View(frame, self.buffer)
            box.preview = self.view
        elif frame.draws_panels and isinstance(ed.options.placement.get("preview"), InPanel):
            # No place of its own: a panel under the text, for as long as this
            # is open.  Only a panel: were the user's placement table to send
            # the preview to the main area, it would take over the window the
            # text is to go into.
            self.view = ed.show(self.buffer, "preview")
            self._panel = getattr(self.view, "panel", None)
        box.on_select.add(self.show)

    def show(self, candidate: Candidate | None) -> None:
        """Write the highlighted entry, whole, in the preview."""
        entry: Any = candidate.value if candidate is not None else None
        text = f"\n{RULE}\n".join(entry.pieces) if isinstance(entry, Entry) else ""
        buffer = self.buffer
        if buffer.text != text:
            buffer.read_only = False
            buffer.replace(0, len(buffer), text)
            buffer.undo.clear()
            buffer.saved_tick = buffer.tick
            buffer.read_only = True
        if self.view is not None and self.view.id in self.view.frame.views:
            self.view.selections.set([(0, 0)])
            self.view.reveal(0, "top")

    def scroll(self, direction: int) -> None:
        view = self.view
        if view is None or view.id not in view.frame.views:
            return
        buffer = self.buffer
        page = max(1, (view.viewport[2] if view.viewport else 10) - 2)
        line = buffer.line_of(view.cursor.pos) + direction * page
        pos = buffer.line_start(max(1, min(buffer.line_count, line)))
        view.selections.set([(pos, pos)])
        view.reveal(pos, "top")

    def close(self) -> None:
        self.box.on_select.remove(self.show)
        view, self.view = self.view, None
        if view is not None and view.id in view.frame.views:
            if self._panel is not None:
                self._panel.close(view)
            else:
                view.close()
        ed.buffers.kill(self.buffer)


def _chooser() -> Chooser:
    box = Minibuffer.of(ed.view)
    found: Any = box.buffer.locals.get("chooser") if box is not None else None
    if not isinstance(found, Chooser):
        raise NeomacsError("Not choosing from the kill ring")
    return found


@command
async def yank_from_ring() -> None:
    """Show the kill ring and insert what is chosen from it.  Straight after
    a yank, the choice replaces what was yanked.

    Typing narrows the list to entries with that text in them.  M-y again
    moves down, Return chooses, C-d removes the entry from the ring, and C-g
    leaves everything as it was."""
    view = ed.view
    frame = ed.frame
    assert frame is not None
    if Minibuffer.of(view) is not None:
        raise NeomacsError("Cannot yank from the ring into a prompt")
    replace = last_yank(view)
    origin, tick = view.buffer, view.buffer.tick
    ring = ed.kill_ring
    await ring.capture(frame)  # what another program has copied since (KILLRING 5.2)
    if not ring.entries:
        ed.message("The kill ring is empty")
        return
    box = Minibuffer(frame, "Yank: ", completer=_Ring(), submit_selects=True)
    box.max_candidates = None  # the whole ring can be walked, however long it is
    box.buffer.set_mode(KillRingMode)
    chooser = Chooser(box, view.buffer)
    box.buffer.locals["chooser"] = chooser
    frame.place(box, PLACE)
    frame.focus_view(box.view)
    box.refresh_candidates()
    answer = box.answer()
    try:
        await ed.dispatcher.wait(answer)
    except Quit:
        if ring.entries:
            raise
        ed.message("The kill ring is empty")  # the last entry was removed from the chooser
        return
    finally:
        chooser.close()
        box.close()
        if answer.done() and not answer.cancelled():
            answer.exception()  # seen: nothing is left to warn about
    chosen = box.chosen
    entry: Any = chosen.value if chosen is not None else None
    if not isinstance(entry, Entry) or view.id not in frame.views:
        return
    if view.buffer is not origin or origin.tick != tick:
        # The text changed while the chooser was open (another frame, a
        # timer): the ranges that were yanked are not where they were.
        replace = None
    ring.take(entry, frame)
    with ed.using(view):
        insert_entry(view, entry, replace)


@command(name="kill_ring.remove")
def remove_entry() -> None:
    """Take the highlighted entry out of the kill ring."""
    box = _chooser().box
    candidate = box.highlighted
    entry: Any = candidate.value if candidate is not None else None
    if not isinstance(entry, Entry):
        raise NeomacsError("No entry is highlighted")
    at = box.selected
    ed.kill_ring.remove(entry)
    if not ed.kill_ring.entries:
        box.cancel()  # nothing left to choose from; `yank_from_ring` says so
        return
    box.candidates = [c for c in box.candidates if c.value is not entry]
    box.selected = max(0, min(at, len(box.candidates) - 1))
    box.frame.popup_changed()
    box.on_select.run(box.highlighted)


@command(name="kill_ring.preview_down")
def preview_down() -> None:
    """Scroll the preview of the highlighted entry forward."""
    _chooser().scroll(1)


@command(name="kill_ring.preview_up")
def preview_up() -> None:
    """Scroll the preview back."""
    _chooser().scroll(-1)
