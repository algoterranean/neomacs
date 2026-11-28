"""The kill ring, and the system clipboard behind the frontend (ENGINE 4.5, 5.7; KILLRING.md)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from neomacs.errors import FrontendGone, NoFrame, RpcError
from neomacs.options import Option
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.window import Frame

CLIPBOARD_TIMEOUT = 0.5

Option("kill_ring_size", int, 60, "How many kills and copies are kept.", validate=lambda n: n >= 1)
Option(
    "kill_ring_entry_limit",
    int,
    1_000_000,
    "Text from the system clipboard longer than this many characters is pasted "
    "when asked for and not kept in the kill ring.",
    validate=lambda n: n >= 1,
)


@provisional("0.1")
@dataclass(eq=False)
class Entry:
    """One thing that was killed or copied: a piece for each selection it was made with."""

    pieces: list[str]
    source: str = "kill"  # "kill", "copy", or "system" for text from another program
    time: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        """The pieces as one text, which is what goes on the system clipboard."""
        return "\n".join(self.pieces)

    def __repr__(self) -> str:
        return f"<Entry {self.source} {self.pieces!r}>"


def _options() -> Any:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    return editor.options if editor is not None else None


@provisional("0.1")
class KillRing:
    """Killed and copied text, oldest first.  The last entry is the top: what `yank` inserts."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        # Text on the system clipboard that is too long to keep.  It is not in
        # the ring, and is still what a yank pastes for as long as the
        # clipboard holds it.
        self._unkept: Entry | None = None

    def _limit(self, name: str, default: int) -> int:
        options = _options()
        return int(options.get(name, default)) if options is not None else default

    @provisional("0.1")
    def push(
        self, pieces: list[str], frame: Frame | None = None, *, source: str = "kill"
    ) -> Entry | None:
        """Add an entry, and put its text on FRAME's system clipboard.

        Every kill is its own entry (K1).  One the same as an entry already
        there is not added again: that entry moves to the top (K6).
        """
        if not any(pieces):
            return None
        pieces = list(pieces)
        entry = next((e for e in self.entries if e.pieces == pieces), None)
        if entry is None:
            entry = Entry(pieces, source)
        else:
            self.entries.remove(entry)
        self.entries.append(entry)
        del self.entries[: -self._limit("kill_ring_size", 60)]
        if frame is not None:
            frame.clipboard_set(entry.text)
        return entry

    @provisional("0.1")
    def take(self, entry: Entry, frame: Frame | None = None) -> None:
        """ENTRY was chosen: it becomes the top, and goes on the system clipboard (K5)."""
        if entry in self.entries:
            self.entries.remove(entry)
        self.entries.append(entry)
        if frame is not None:
            frame.clipboard_set(entry.text)

    @provisional("0.1")
    def remove(self, entry: Entry) -> None:
        if entry in self.entries:
            self.entries.remove(entry)

    @property
    @provisional("0.1")
    def top(self) -> Entry | None:
        return self.entries[-1] if self.entries else None

    @provisional("0.1")
    async def capture(self, frame: Frame | None) -> Entry | None:
        """Look at FRAME's system clipboard.  Text that another program put
        there since this frame last looked becomes the top entry.

        Returns what the clipboard holds if it is news, whether or not it was
        kept: text over `kill_ring_entry_limit` is returned, for a yank to
        paste, and not added to the ring.
        """
        if frame is None or not frame.attached:
            return None
        text: Any = None
        try:
            reply: Any = await frame.request(
                "clipboard.get", "clipboard", ["text"], timeout=CLIPBOARD_TIMEOUT
            )
            if isinstance(reply, dict):
                text = cast(dict[str, Any], reply).get("text")
        except RpcError, FrontendGone, NoFrame, TimeoutError:
            return None
        if not isinstance(text, str) or not text or text == frame.last_clipboard:
            return None
        frame.last_clipboard = text
        self._unkept = None
        if len(text) > self._limit("kill_ring_entry_limit", 1_000_000):
            self._unkept = Entry([text], "system")
            return self._unkept
        # Not put back on the clipboard it came from.
        return self.push([text], source="system")

    @provisional("0.1")
    async def current(self, frame: Frame | None) -> Entry | None:
        """The entry to yank: what another program has put on the system
        clipboard since the engine last looked, otherwise the top of the ring."""
        found = await self.capture(frame)
        if found is not None:
            return found
        # Seen before, by a yank or when the window regained focus, and too
        # long to have been kept: it is still what the clipboard holds.
        unkept = self._unkept
        if unkept is not None and frame is not None and frame.last_clipboard == unkept.text:
            return unkept
        return self.top

    @provisional("0.1")
    def distribute(self, entry: Entry, count: int) -> list[str]:
        """What each of COUNT selections receives from ENTRY."""
        if len(entry.pieces) == count:
            return list(entry.pieces)
        return [entry.text] * count
