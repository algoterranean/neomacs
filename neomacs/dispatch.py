"""Input handling.

Stage 2 form: keys are handled one at a time, in arrival order, by a
hard-wired table.  The command system (ENGINE 7, 8.1) replaces `handle_key`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.window import Frame


class InputQueue:
    """Input events from every frame, handled strictly in order."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self._queue: asyncio.Queue[tuple[Frame, str, Any]] | None = None
        self._active = False

    def start(self) -> None:
        loop = self.editor.loop
        assert loop is not None
        self._queue = asyncio.Queue()
        self.editor.keep_task(loop.create_task(self._worker()))

    def put(self, frame: Frame, kind: str, value: Any) -> None:
        if self._queue is not None:
            self._queue.put_nowait((frame, kind, value))

    @property
    def busy(self) -> bool:
        return self._active or (self._queue is not None and not self._queue.empty())

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            frame, kind, value = await self._queue.get()
            self._active = True
            try:
                await self.editor.aguard(f"handling {kind} {value!r}", handle(frame, kind, value))
            finally:
                self._active = False
                frame.schedule_update()


def is_printable(key: str) -> bool:
    """True if KEY is text to insert: not a named key and not modified."""
    if len(key) >= 3 and key[0] == "<" and key[-1] == ">":
        return False
    return not (len(key) >= 3 and key[1] == "-" and key[0] in "CMSs")


async def handle(frame: Frame, kind: str, value: Any) -> None:
    view = frame.focus
    if kind == "text":
        view.goal_x = None
        view.cursor.insert(str(value))
        view.collapse()
        view.reveal()
    elif kind == "key":
        await handle_key(frame, str(value))


async def handle_key(frame: Frame, key: str) -> None:
    view = frame.focus
    buf = view.buffer
    cur = view.cursor
    if key in ("<up>", "C-p"):
        await view.move_visual(-1)
    elif key in ("<down>", "C-n"):
        await view.move_visual(1)
    elif key in ("<prior>", "M-v"):
        await view.move_visual(-1, "page")
    elif key in ("<next>", "C-v"):
        await view.move_visual(1, "page")
    else:
        view.goal_x = None
        if key in ("<left>", "C-b"):
            cur.backward_char()
        elif key in ("<right>", "C-f"):
            cur.forward_char()
        elif key in ("<home>", "C-a"):
            cur.start_of_line()
        elif key in ("<end>", "C-e"):
            cur.end_of_line()
        elif key == "M-<":
            cur.move_to(0)
        elif key == "M->":
            cur.move_to(len(buf))
        elif key == "<backspace>":
            cur.delete_backward_char()
        elif key in ("<delete>", "C-d"):
            cur.delete_char()
        elif key == "<return>":
            cur.insert("\n")
        elif key == "<tab>":
            cur.insert("\t")
        elif key == "<space>":
            cur.insert(" ")
        elif key in ("C-/", "C-_"):
            group = buf.undo.undo()
            if group is not None and group.edits:
                cur.move_to(group.edits[0].pos + len(group.edits[0].removed))
        elif key == "M-/":
            group = buf.undo.redo()
            if group is not None and group.edits:
                cur.move_to(group.edits[-1].pos + len(group.edits[-1].inserted))
        elif is_printable(key):
            cur.insert(key)
        else:
            frame.editor.message(f"{key} is undefined")
            return
    view.collapse()
    view.reveal()
