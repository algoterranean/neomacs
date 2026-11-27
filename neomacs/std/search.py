"""Standard commands: incremental search, and a cursor on every match."""

from __future__ import annotations

from typing import Any

from neomacs import keys as keys_module
from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap
from neomacs.live import keep
from neomacs.text.buffer import Buffer

LAYER = "search"
MAX_MARKED = 400  # matches given a face at once

# What each key does during an incremental search.  A key that is not here
# and is not text ends the search and then does what it normally does.
search_map = Keymap("search")
for _key, _action in [
    ("C-s", "next"),
    ("C-r", "previous"),
    ("<down>", "next"),
    ("<up>", "previous"),
    ("<backspace>", "delete"),
    ("<return>", "accept"),
    ("C-g", "cancel"),
    ("<escape>", "cancel"),
    ("C-w", "word"),
    ("M-n", "add"),
    ("M-a", "all"),
]:
    search_map.bind(_key, _action)


def _folds(pattern: str) -> bool:
    """Case is ignored unless the pattern has a capital in it."""
    return pattern == pattern.lower()


def find_all(buffer: Buffer, pattern: str, *, whole_word: bool = False) -> list[tuple[int, int]]:
    """Every occurrence of PATTERN in BUFFER, without overlaps."""
    if not pattern:
        return []
    text = buffer.text
    if _folds(pattern):
        folded = text.lower()
        text = folded if len(folded) == len(text) else text  # a few characters change length
    table = buffer.syntax.table
    found: list[tuple[int, int]] = []
    at = text.find(pattern)
    size = len(pattern)
    while at != -1:
        end = at + size
        if not whole_word or (
            (at == 0 or not table.is_word(text[at - 1]))
            and (end >= len(text) or not table.is_word(text[end]))
        ):
            found.append((at, end))
            at = text.find(pattern, end)
        else:
            at = text.find(pattern, at + 1)
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


async def _search(direction: int) -> None:
    view = ed.view
    frame = ed.frame
    assert frame is not None
    buffer = view.buffer
    origin = view.selections.ranges()
    origin_primary = view.selections.primary_index
    start_pos = view.cursor.pos
    history = keep("search.history", list[str])
    pattern = ""
    matches: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    failed = False

    def locate(from_pos: int, step: int, *, wrap: bool) -> None:
        nonlocal current, failed
        if not matches:
            current, failed = None, bool(pattern)
            return
        if step > 0:
            later = [m for m in matches if m[0] >= from_pos]
            current = later[0] if later else (matches[0] if wrap else None)
        else:
            earlier = [m for m in matches if m[1] <= from_pos]
            current = earlier[-1] if earlier else (matches[-1] if wrap else None)
        failed = current is None
        if current is None and matches:
            current = None

    def index_of(match: tuple[int, int] | None) -> int:
        return matches.index(match) if match is not None and match in matches else 0

    def show() -> None:
        _mark(buffer, matches, current)
        if current is not None:
            view.selections.set([(current[0], current[1])])
            view.reveal(current[0], "nearest")
        elif not pattern:
            view.selections.set(origin, origin_primary)
        count = f"  [{index_of(current) + 1}/{len(matches)}]" if current is not None else ""
        label = (
            "Failing search: " if failed else ("Search backward: " if direction < 0 else "Search: ")
        )
        ed.message(label + pattern + count, kind="keys")

    def done(*, restore: bool) -> None:
        buffer.layer(LAYER).clear()
        frame.clear_keys()
        if restore:
            view.selections.set(origin, origin_primary)
            view.reveal()
        elif pattern:
            if not history or history[-1] != pattern:
                history.append(pattern)
            view.jumps.push(buffer, start_pos)

    show()
    try:
        while True:
            key = await ed.read_key()
            action = search_map.lookup([key])
            text = keys_module.text_of(key)
            if isinstance(action, Keymap):
                action = None
            if action is None and text is not None:
                pattern += text
                matches = find_all(buffer, pattern)
                anchor = current[0] if current is not None else start_pos
                locate(anchor if direction > 0 else anchor + len(pattern), direction, wrap=False)
            elif action == "delete":
                pattern = pattern[:-1]
                matches = find_all(buffer, pattern)
                locate(start_pos, direction, wrap=True)
            elif action in ("next", "previous"):
                direction = 1 if action == "next" else -1
                if not pattern and history:
                    pattern = history[-1]
                    matches = find_all(buffer, pattern)
                    locate(start_pos, direction, wrap=True)
                elif current is not None:
                    locate(current[1] if direction > 0 else current[0], direction, wrap=True)
                else:
                    locate(start_pos, direction, wrap=True)
            elif action == "word":
                at = current[1] if current is not None else view.cursor.pos
                end = buffer.syntax.next_word_end(at)
                pattern += buffer[at:end].lower() if _folds(pattern) else buffer[at:end]
                matches = find_all(buffer, pattern)
                locate(current[0] if current is not None else start_pos, 1, wrap=True)
            elif action == "all":
                done(restore=False)
                if matches:
                    view.selections.set(
                        [(a, b) for a, b in matches],
                        index_of(current),
                    )
                    ed.message(f"{len(matches)} selections")
                return
            elif action == "add":
                # Keep this match selected and go on to the next one.
                if current is not None:
                    kept = [r for r in view.selections.ranges() if r != (current[0], current[1])]
                    locate(current[1], 1, wrap=True)
                    extra = kept + [(m[0], m[1]) for m in [current] if m is not None]
                    _mark(buffer, matches, current)
                    view.selections.set(sorted(set(extra)))
                    continue
            elif action == "accept":
                done(restore=False)
                return
            elif action == "cancel":
                done(restore=True)
                ed.message("Quit")
                return
            else:
                # Any other key ends the search and is then handled as usual.
                done(restore=False)
                ed.dispatcher.put(frame, "key", key)
                return
            show()
    except BaseException:
        done(restore=True)
        raise


@command
async def search_forward() -> None:
    """Search as you type.  C-s and C-r go to the next and previous match,
    Return stays there, C-g goes back to where the search began."""
    await _search(1)


@command
async def search_backward() -> None:
    """Search backward as you type."""
    await _search(-1)


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
