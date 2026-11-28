"""A side-by-side diff: two windows, the lines that differ marked in each
(WORKSPACE 8).

The two sides are ordinary buffers in ordinary windows.  What makes them a
diff is kept here: which lines of one answer which of the other, the marks,
and the rule that scrolling one side brings the other along.  The rows are
not kept level (W11): a side scrolls to the line that matches, no more.
"""

from __future__ import annotations

import difflib
import time
from typing import TYPE_CHECKING, Any

from neomacs.text.buffer import Buffer, Change

if TYPE_CHECKING:
    from neomacs.window import Frame, Window

LAYER = "diff"
# Above this many lines in the two sides together the marks are not worked
# out: comparing would hold the editor up.
MAX_LINES = 40_000
RECOMPUTE_AFTER = 0.3  # seconds after the last edit
# A side that was just scrolled to keep up says so in a viewport report of
# its own, which must not be taken for the user scrolling it.
FOLLOW_QUIET = 0.3

_FACES = {"delete": "diff-removed", "insert": "diff-added", "replace": "diff-changed"}
_MARKS = {"delete": ("-", "error"), "insert": ("+", "added"), "replace": ("~", "warning")}

# One block of the comparison: (what happened, old lines [a1, a2), new lines
# [b1, b2)), counted from 0.
Block = tuple[str, int, int, int, int]


def compare(old: str, new: str) -> list[Block]:
    """The blocks that make NEW of OLD, line by line, the unchanged ones included."""
    a, b = old.split("\n"), new.split("\n")
    return list(difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes())


class Diff:
    """OLD in one window beside NEW in another."""

    def __init__(
        self, frame: Frame, old: Window, new: Window, *, title: str, temporary: list[Buffer]
    ) -> None:
        self.frame = frame
        self.old = old
        self.new = new
        self.old_buffer: Buffer = old.buffer
        self.new_buffer: Buffer = new.buffer
        self.title = title
        # Buffers made for this diff, which go when it does.
        self.temporary = temporary
        self.blocks: list[Block] = []
        self.too_big = False
        self._timer: Any = None
        self._followed: dict[int, float] = {}
        self.ended = False
        # Marks of this diff only: another frame may be comparing the same file.
        self.layer = f"{LAYER}:{frame.id}"
        self.new_buffer.add_listener(self._changed)
        self.compute()

    # -- the comparison ------------------------------------------------------------

    def compute(self) -> None:
        """Compare the two sides as they are now, and mark what differs."""
        old, new = self.old_buffer, self.new_buffer
        self.too_big = old.line_count + new.line_count > MAX_LINES
        self.blocks = [] if self.too_big else compare(old.text, new.text)
        for buffer, side in ((old, 0), (new, 1)):
            layer = buffer.layer(self.layer)
            layer.clear()
            for tag, a1, a2, b1, b2 in self.blocks:
                if tag == "equal":
                    continue
                first, last = (a1, a2) if side == 0 else (b1, b2)
                mark, mark_face = _MARKS[tag]
                for line in range(first + 1, min(last, buffer.line_count) + 1):
                    start = buffer.line_start(line)
                    layer.add(
                        start,
                        buffer.line_end(line),
                        line={"face": _FACES[tag]},
                        gutter={"column": "signs", "text": mark, "face": mark_face},
                    )
        self.frame.editor.update_frames()

    def changes(self) -> list[Block]:
        """The blocks where the sides differ."""
        return [block for block in self.blocks if block[0] != "equal"]

    def _changed(self, change: Change) -> None:
        if not change.text_changed:
            return  # a redraw, such as the marks themselves ask for
        # Not at every key: once the typing has paused.
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self.frame.editor.after(RECOMPUTE_AFTER, self._recompute)

    def _recompute(self) -> None:
        self._timer = None
        if not self.ended:
            self.compute()

    # -- the two sides keep together ---------------------------------------------------

    def side(self, view: Any) -> int | None:
        """0 if VIEW is the old side, 1 if the new, None if neither."""
        return 0 if view is self.old else 1 if view is self.new else None

    def matching_line(self, side: int, line: int) -> int:
        """The line of the other side that answers LINE (from 1) of SIDE: the
        same line where they agree, the start of the answering block where
        they differ."""
        at = line - 1
        for _, a1, a2, b1, b2 in self.blocks:
            mine, theirs = ((a1, a2), (b1, b2)) if side == 0 else ((b1, b2), (a1, a2))
            if mine[0] <= at < mine[1]:
                inside = at - mine[0]
                return theirs[0] + min(inside, max(0, theirs[1] - theirs[0] - 1)) + 1
        other = self.new_buffer if side == 0 else self.old_buffer
        return max(1, min(other.line_count, line))

    def scrolled(self, view: Any) -> None:
        """VIEW, one of the sides, reports a new first line: if that was the
        user scrolling it, the other side goes to the line that matches."""
        side = self.side(view)
        if side is None or self.ended or view.viewport is None:
            return
        if time.monotonic() - self._followed.get(view.id, 0.0) < FOLLOW_QUIET:
            return  # it moved because the other side did
        other = self.new if side == 0 else self.old
        if other.id not in self.frame.views:
            return
        line = self.matching_line(side, view.viewport[0])
        line = max(1, min(other.buffer.line_count, line))
        if other.viewport is not None and other.viewport[0] == line:
            return
        self._followed[other.id] = time.monotonic()
        other.reveal(other.buffer.line_start(line), "top")

    # -- its life ----------------------------------------------------------------------

    def alive(self) -> bool:
        """Whether both windows are still there, each showing its side."""
        frame = self.frame
        return (
            not self.ended
            and self.old in frame.windows()
            and self.new in frame.windows()
            and self.old.buffer is self.old_buffer
            and self.new.buffer is self.new_buffer
        )

    def end(self, *, close: bool = False) -> None:
        """Stop being a diff: the marks go, and the buffers made for it.  With
        CLOSE the old side's window goes too, if it is still there."""
        if self.ended:
            return
        self.ended = True
        frame = self.frame
        if frame.diff is self:
            frame.diff = None
        if self._timer is not None:
            self._timer.cancel()
        self.new_buffer.remove_listener(self._changed)
        for buffer in (self.old_buffer, self.new_buffer):
            buffer.layer(self.layer).clear()
        on_screen = self.old.id in frame.views and self.old in frame.windows()
        if close and on_screen and len(frame.windows()) > 1 and self.old.buffer is self.old_buffer:
            frame.delete_window(self.old)
        for buffer in self.temporary:
            frame.editor.buffers.kill(buffer)
        frame.editor.update_frames()


def check(frame: Frame) -> None:
    """End FRAME's diff if one of its windows has gone or shows something else."""
    diff: Diff | None = frame.diff
    if diff is not None and not diff.alive():
        diff.end()
