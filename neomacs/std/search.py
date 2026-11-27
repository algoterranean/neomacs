"""Standard commands: incremental search, and a cursor on every match."""

from __future__ import annotations

import re
from typing import Any

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap
from neomacs.live import keep
from neomacs.minibuffer import Minibuffer, MinibufferMode
from neomacs.text.buffer import Buffer
from neomacs.window import View

LAYER = "search"
MAX_MARKED = 400  # matches given a face at once
PLACE = "search"  # the part of a frontend's window the search box goes in, if it has one


class SearchMode(MinibufferMode):
    """The mode of the search box.  What is typed is the pattern; these keys
    move among its matches."""

    name = "search"
    interactive = False
    keymap = Keymap("search")


assert SearchMode.keymap is not None
search_map = SearchMode.keymap
for _key, _name in [
    ("C-s", "search.next"),
    ("C-r", "search.previous"),
    ("<down>", "search.next"),
    ("<up>", "search.previous"),
    ("C-w", "search.word"),
    # Not M-n, the key for this outside a search: here it and M-p go through
    # the patterns searched for before.
    ("M-m", "search.add"),
    ("s-d", "search.add"),
    ("M-a", "search.all"),
]:
    search_map.bind(_key, _name)


def _folds(pattern: str) -> bool:
    """Case is ignored unless the pattern has a capital in it."""
    return pattern == pattern.lower()


def find_all(buffer: Buffer, pattern: str, *, whole_word: bool = False) -> list[tuple[int, int]]:
    """Every occurrence of PATTERN in BUFFER, without overlaps."""
    if not pattern:
        return []
    text = buffer.text
    # Matched against the text as it is, so positions are right even where
    # changing a character's case would change its length.
    flags = re.IGNORECASE if _folds(pattern) else 0
    table = buffer.syntax.table
    found: list[tuple[int, int]] = []
    at = 0
    compiled = re.compile(re.escape(pattern), flags)
    while (match := compiled.search(text, at)) is not None:
        start, end = match.span()
        if not whole_word or (
            (start == 0 or not table.is_word(text[start - 1]))
            and (end >= len(text) or not table.is_word(text[end]))
        ):
            found.append((start, end))
            at = end
        else:
            at = start + 1
    return found


def _mark(buffer: Buffer, matches: list[tuple[int, int]], current: tuple[int, int] | None) -> None:
    layer = buffer.layer(LAYER)
    layer.clear()
    if current is not None:
        layer.add(*current, priority=30, face="match-current")
    near = current[0] if current is not None else 0
    for start, end in sorted(matches, key=lambda m: abs(m[0] - near))[:MAX_MARKED]:
        if (start, end) != current:
            layer.add(start, end, priority=25, face="match")


class Search:
    """One incremental search: a box to type the pattern in, and the view being searched."""

    def __init__(self, view: View, box: Minibuffer, direction: int) -> None:
        self.view = view
        self.buffer = view.buffer
        self.box = box
        self.direction = direction
        self.origin = view.selections.ranges()
        self.origin_primary = view.selections.primary_index
        self.start = view.cursor.pos
        self.pattern = ""
        self.matches: list[tuple[int, int]] = []
        self.current: tuple[int, int] | None = None
        self.added: list[tuple[int, int]] = []  # matches kept selected with `add`
        self.failed = False
        self.tick = self.buffer.tick  # the text the matches were found in
        self.resume = self.start  # where to go on from when not on a match

    def refresh(self) -> None:
        """Find the matches again if the searched text has changed since they
        were found: the box may be left for the text and come back to.  Called
        before anything uses the matches, and before the text is displayed."""
        buffer = self.buffer
        if not buffer.changed_since(self.tick):
            return
        self.tick = buffer.tick
        view = self.view
        if view.id not in view.frame.views or view.buffer is not buffer:
            return
        self.matches = find_all(buffer, self.pattern)
        found = set(self.matches)
        # The selections moved with the text.  One still on a match is still
        # the match the search is on, or one kept with `add`.
        primary = view.selections.primary
        here = (primary.start, primary.end)
        self.current = here if here in found else None
        self.added = [r for s in view.selections if (r := (s.start, s.end)) in found and r != here]
        self.resume = view.cursor.pos
        self.failed = bool(self.pattern) and not self.matches
        size = len(buffer)
        self.origin = [(min(a, size), min(h, size)) for a, h in self.origin]
        self.start = min(self.start, size)
        self.mark()

    def locate(self, from_pos: int, step: int, *, wrap: bool) -> None:
        matches = self.matches
        if not matches:
            self.current, self.failed = None, bool(self.pattern)
            return
        if step > 0:
            later = [m for m in matches if m[0] >= from_pos]
            self.current = later[0] if later else (matches[0] if wrap else None)
        else:
            earlier = [m for m in matches if m[1] <= from_pos]
            self.current = earlier[-1] if earlier else (matches[-1] if wrap else None)
        self.failed = self.current is None

    def changed(self, text: str) -> None:
        """The pattern in the box changed: find it again, from where the search is."""
        if text == self.pattern:
            return
        grew = text.startswith(self.pattern) and self.pattern != ""
        self.refresh()
        self.pattern = text
        self.matches = find_all(self.buffer, text)
        self.added.clear()  # they matched the pattern as it was
        if grew and self.current is not None:
            anchor = self.current[0] if self.direction > 0 else self.current[0] + len(text)
            self.locate(anchor, self.direction, wrap=False)
        else:
            self.locate(self.start, self.direction, wrap=True)
        self.show()

    def step(self, direction: int) -> None:
        self.refresh()
        self.direction = direction
        if not self.pattern:
            history: list[str] = keep("search.history", list[str])
            if history:
                self.box.text = history[-1]  # the last thing searched for
            return
        if self.current is not None:
            self.locate(self.current[1] if direction > 0 else self.current[0], direction, wrap=True)
        else:
            self.locate(self.resume, direction, wrap=True)
        self.show()

    def show(self) -> None:
        view, current = self.view, self.current
        if current is not None:
            # The matches kept with `add`, and the one the search is on now.
            ranges = sorted({*self.added, current})
            view.selections.set(ranges, ranges.index(current))
            view.reveal(current[0], "nearest")
        elif not self.pattern:
            view.selections.set(self.origin, self.origin_primary)
        self.mark()

    def mark(self) -> None:
        """Give the matches their faces, and say in the box's label how the search is going."""
        current = self.current
        _mark(self.buffer, self.matches, current)
        if self.failed:
            label = "No match: "
        elif current is not None:
            label = f"Search [{self.matches.index(current) + 1}/{len(self.matches)}]: "
        else:
            label = "Search backward: " if self.direction < 0 else "Search: "
        if self.box.label != label:
            self.box.label = label
            self.box.frame.schedule_update()

    def finish(self, *, restore: bool) -> None:
        self.refresh()
        self.buffer.layer(LAYER).clear()
        view = self.view
        if view.id not in view.frame.views or view.buffer is not self.buffer:
            return
        if restore:
            view.selections.set(self.origin, self.origin_primary)
            view.reveal()
        elif self.pattern:
            history: list[str] = keep("search.history", list[str])
            if not history or history[-1] != self.pattern:
                history.append(self.pattern)
            view.jumps.push(self.buffer, self.start)


def _session() -> Search:
    box = Minibuffer.of(ed.view)
    found: Any = box.buffer.locals.get("search") if box is not None else None
    if not isinstance(found, Search):
        raise NeomacsError("Not searching")
    return found


async def _search(direction: int) -> None:
    view = ed.view
    frame = ed.frame
    assert frame is not None
    if Minibuffer.of(view) is not None:
        raise NeomacsError("Cannot search in a prompt")
    # A box of its own to type the pattern in: real text, edited like any other.
    # A frontend with a place for it shows it there; otherwise it is a prompt.
    box = Minibuffer(frame, "Search backward: " if direction < 0 else "Search: ", history="search")
    box.buffer.set_mode(SearchMode)
    search = Search(view, box, direction)
    box.buffer.locals["search"] = search
    box.on_change.add(search.changed)
    search.buffer.display_preparers.append(search.refresh)
    frame.place(box, PLACE)
    frame.focus_view(box.view)
    answer = box.answer()
    accepted = False
    try:
        await ed.dispatcher.wait(answer)
        accepted = True
    finally:
        box.close()
        # From the buffer searched: the view may be showing another by now.
        if search.refresh in search.buffer.display_preparers:
            search.buffer.display_preparers.remove(search.refresh)
        search.finish(restore=not accepted)
        if answer.done() and not answer.cancelled():
            answer.exception()  # seen: nothing is left to warn about


@command
async def search_forward() -> None:
    """Search as you type, in a box of its own.  C-s and C-r go to the next and
    previous match, Return stays there, C-g goes back to where the search began."""
    await _search(1)


@command
async def search_backward() -> None:
    """Search backward as you type."""
    await _search(-1)


@command(name="search.next")
def search_next() -> None:
    """Go to the next match.  With nothing typed yet, search for the last thing again."""
    _session().step(1)


@command(name="search.previous")
def search_previous() -> None:
    """Go to the previous match."""
    _session().step(-1)


@command(name="search.word")
def search_word() -> None:
    """Add the rest of the word after the match to the pattern."""
    search = _session()
    search.refresh()
    at = search.current[1] if search.current is not None else search.view.cursor.pos
    end = search.buffer.syntax.next_word_end(at)
    more = search.buffer[at:end]
    search.box.text = search.pattern + (more.lower() if _folds(search.pattern) else more)


@command(name="search.add")
def search_add() -> None:
    """Keep this match selected and go on to the next one."""
    search = _session()
    search.refresh()
    if search.current is None:
        raise NeomacsError("No match to keep")
    search.added.append(search.current)
    search.locate(search.current[1], 1, wrap=True)
    search.show()


@command(name="search.all")
def search_all() -> None:
    """End the search with a selection on every match."""
    search = _session()
    search.refresh()
    matches, current = list(search.matches), search.current
    if not matches:
        raise NeomacsError("No matches")
    search.added = [m for m in matches if m != current]
    if current is None:
        search.current = matches[0]
    search.show()
    search.box.submit()
    ed.message(f"{len(matches)} selection{'s' if len(matches) != 1 else ''}")


def _subject(view: Any) -> tuple[str, bool]:
    """What to look for: the primary selection's text, or the word at the cursor."""
    primary = view.selections.primary
    if not primary.empty:
        return primary.text, False
    word = view.buffer.syntax.word_at(primary.head.pos)
    if word is None:
        raise NeomacsError("No word here")
    return view.buffer[word[0] : word[1]], True


@command
def add_next_match() -> None:
    """Select the word at the cursor; then, each time, also select its next occurrence."""
    view = ed.view
    buffer = view.buffer
    primary = view.selections.primary
    if primary.empty:
        word = buffer.syntax.word_at(primary.head.pos)
        if word is None:
            raise NeomacsError("No word here")
        primary.select(*word)
        return
    pattern = primary.text
    taken = {(s.start, s.end) for s in view.selections}
    text = buffer.text
    last = max(end for _, end in taken)
    at = text.find(pattern, last)
    if at == -1:
        at = text.find(pattern)  # wrap to the top
    while at != -1 and (at, at + len(pattern)) in taken:
        at = text.find(pattern, at + len(pattern))
    if at == -1:
        raise NeomacsError("No more matches")
    view.selections.add(at + len(pattern), at)
    view.reveal(at)


@command
def select_all_matches() -> None:
    """Put a selection on every occurrence of the selected text, or of the word at the cursor."""
    view = ed.view
    pattern, whole_word = _subject(view)
    text = view.buffer.text
    found: list[tuple[int, int]] = []
    table = view.buffer.syntax.table
    at = text.find(pattern)
    while at != -1:
        end = at + len(pattern)
        inside = whole_word and (
            (at > 0 and table.is_word(text[at - 1]))
            or (end < len(text) and table.is_word(text[end]))
        )
        if not inside:
            found.append((at, end))
        at = text.find(pattern, end)
    here = view.cursor.pos
    primary = next((i for i, (a, b) in enumerate(found) if a <= here <= b), 0)
    view.selections.set(found, primary)
    ed.message(f"{len(found)} selection{'s' if len(found) != 1 else ''}")
