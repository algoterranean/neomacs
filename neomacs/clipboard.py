"""The kill ring, and the system clipboard behind the frontend (ENGINE 4.5, 5.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from neomacs.errors import FrontendGone, NoFrame, RpcError
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.window import Frame

RING_SIZE = 60
CLIPBOARD_TIMEOUT = 0.5


@provisional("0.1")
class KillRing:
    """Killed and copied text.  An entry is one piece per selection that was killed."""

    def __init__(self) -> None:
        self.entries: list[list[str]] = []

    @provisional("0.1")
    def push(self, pieces: list[str], frame: Frame | None = None) -> None:
        """Add an entry, and put its text on FRAME's system clipboard."""
        if not any(pieces):
            return
        self.entries.append(list(pieces))
        del self.entries[:-RING_SIZE]
        if frame is not None:
            frame.clipboard_set("\n".join(pieces))

    @property
    @provisional("0.1")
    def top(self) -> list[str] | None:
        return self.entries[-1] if self.entries else None

    @provisional("0.1")
    async def current(self, frame: Frame | None) -> list[str] | None:
        """The entry to yank: the system clipboard if another program has put
        something there since the engine last did, otherwise the top of the ring."""
        if frame is not None and frame.attached:
            text: Any = None
            try:
                reply: Any = await frame.request(
                    "clipboard.get", "clipboard", ["text"], timeout=CLIPBOARD_TIMEOUT
                )
                if isinstance(reply, dict):
                    text = cast(dict[str, Any], reply).get("text")
            except RpcError, FrontendGone, NoFrame, TimeoutError:
                text = None
            if isinstance(text, str) and text and text != frame.last_clipboard:
                frame.last_clipboard = text
                self.entries.append([text])
                del self.entries[:-RING_SIZE]
        return self.top

    @provisional("0.1")
    def distribute(self, entry: list[str], count: int) -> list[str]:
        """What each of COUNT selections receives from ENTRY."""
        if len(entry) == count:
            return list(entry)
        return ["\n".join(entry)] * count
