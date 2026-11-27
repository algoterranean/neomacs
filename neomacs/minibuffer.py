"""Minibuffers (ENGINE 7.5): text inputs that are not document buffers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

from neomacs.completion import Candidate, Completer
from neomacs.errors import NoFrame, Quit
from neomacs.hooks import Hook
from neomacs.keymap import Keymap
from neomacs.modes import Mode
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer, Change
from neomacs.window import Frame, View

if TYPE_CHECKING:
    from neomacs.editor import Editor

MAX_CANDIDATES = 50


class MinibufferMode(Mode):
    """The major mode of a single-line minibuffer."""

    name = "minibuffer"
    interactive = False
    keymap: ClassVar[Keymap | None] = Keymap("minibuffer")


class MultilineMinibufferMode(MinibufferMode):
    """The major mode of a multi-line minibuffer: <return> inserts a newline."""

    name = "minibuffer-multiline"
    keymap: ClassVar[Keymap | None] = Keymap("minibuffer-multiline")


assert MinibufferMode.keymap is not None and MultilineMinibufferMode.keymap is not None
for _key, _command in [
    ("<return>", "minibuffer.submit"),
    ("C-<return>", "minibuffer.submit"),
    ("C-g", "minibuffer.cancel"),
    ("<escape>", "minibuffer.cancel"),
    ("M-p", "minibuffer.history_previous"),
    ("M-n", "minibuffer.history_next"),
    ("<up>", "minibuffer.previous"),
    ("<down>", "minibuffer.next"),
    ("C-p", "minibuffer.previous"),
    ("C-n", "minibuffer.next"),
    ("<tab>", "minibuffer.complete"),
]:
    MinibufferMode.keymap.bind(_key, _command)
MultilineMinibufferMode.keymap.bind("<return>", "newline")
MultilineMinibufferMode.keymap.bind("<up>", "previous_line")
MultilineMinibufferMode.keymap.bind("<down>", "next_line")
MultilineMinibufferMode.keymap.bind("C-p", "previous_line")
MultilineMinibufferMode.keymap.bind("C-n", "next_line")


class MinibufferView(View):
    """The view of a minibuffer's buffer.  It is not in the window tree."""


@provisional("0.1")
class Minibuffer:
    def __init__(
        self,
        frame: Frame,
        label: str = "",
        *,
        multiline: bool = False,
        min_lines: int = 1,
        max_lines: int = 10,
        placeholder: str = "",
        masked: bool = False,
        completer: Completer | None = None,
        history: str | None = None,
        initial: str = "",
        submit_selects: bool = False,
    ) -> None:
        self.frame = frame
        self.label = label
        self.multiline = multiline
        self.min_lines = min_lines
        self.max_lines = max_lines
        self.placeholder = placeholder
        self.masked = masked
        self.completer = completer
        self.history = history
        # Submitting takes the highlighted candidate rather than the text as typed.
        self.submit_selects = submit_selects
        self.host: tuple[Any, ...] = ()
        self.candidates: list[Candidate] = []
        self.selected = 0
        self.on_submit = Hook(
            "minibuffer-submit", "Run with (text) on each submit.", register=False
        )
        self.on_change = Hook(
            "minibuffer-change", "Run with (text) on each change.", register=False
        )
        self.buffer = Buffer("*minibuffer*", initial)
        self.buffer.undo.clear()
        self.buffer.set_mode(MultilineMinibufferMode if multiline else MinibufferMode)
        self.buffer.locals["minibuffer"] = self
        self.buffer.option_values["gutter_columns"] = []
        if not multiline:
            self.buffer.text_filter = lambda text: text.replace("\n", " ")
        self.view = MinibufferView(frame, self.buffer)
        self.view.selections.primary.move_to(len(self.buffer))
        self._waiters: list[asyncio.Future[str]] = []
        self._history_index: int | None = None
        self._completion: asyncio.Task[None] | None = None
        self._previous_focus: View | None = None
        self.closed = False
        self.buffer.add_listener(self._changed)

    @staticmethod
    def of(view: View) -> Minibuffer | None:
        """The minibuffer VIEW belongs to, if it is one."""
        found = view.buffer.locals.get("minibuffer")
        return found if isinstance(found, Minibuffer) else None

    @property
    @provisional("0.1")
    def text(self) -> str:
        return self.buffer.text

    @text.setter
    def text(self, value: str) -> None:
        self.buffer.replace(0, len(self.buffer), value)
        self.view.selections.set([(len(self.buffer), len(self.buffer))])

    def options(self) -> dict[str, Any]:
        return {
            "label": [[None, self.label, 0]] if self.label else [],
            "multiline": self.multiline,
            "min_lines": self.min_lines,
            "max_lines": self.max_lines,
            "placeholder": self.placeholder,
            "masked": self.masked,
        }

    # -- submitting -------------------------------------------------------------

    @provisional("0.1")
    def submit(self) -> None:
        text = self.text
        if self.submit_selects and self.candidates:
            exact = any(c.text == text for c in self.candidates)
            if not exact:
                text = self.candidates[min(self.selected, len(self.candidates) - 1)].text
        if self.history is not None and text:
            entries = self.frame.editor.histories.setdefault(self.history, [])
            if not entries or entries[-1] != text:
                entries.append(text)
        self._history_index = None
        self.on_submit.run(text)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(text)
        self._close_if_reader_is_frozen(waiters)

    @provisional("0.1")
    def cancel(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(Quit("cancelled"))
        self._close_if_reader_is_frozen(waiters)

    def _close_if_reader_is_frozen(self, waiters: list[asyncio.Future[str]]) -> None:
        """Normally whoever asked closes the prompt when it has its answer.  If
        that code is on the loop the debugger has frozen, it cannot yet; take
        the prompt off the screen now."""
        if self.closed or not waiters:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if any(waiter.get_loop() is not running for waiter in waiters):
            self.close()

    def answer(self) -> asyncio.Future[str]:
        """A future for the next submitted text; it fails with Quit on cancel."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        return future

    @provisional("0.1")
    async def result(self) -> str:
        """The next submitted text.  Raises Quit if the minibuffer is cancelled."""
        return await self.answer()

    @provisional("0.1")
    def close(self) -> None:
        """Remove the minibuffer from wherever it is shown."""
        if self.closed:
            return
        self.closed = True
        self.cancel()
        self.buffer.remove_listener(self._changed)
        if self._completion is not None:
            self._completion.cancel()
        self.frame.remove_minibuffer(self)

    # -- history ------------------------------------------------------------------

    def history_step(self, delta: int) -> None:
        entries = self.frame.editor.histories.get(self.history or "", [])
        if not entries:
            return
        index = len(entries) if self._history_index is None else self._history_index
        index = max(0, min(len(entries), index + delta))
        self._history_index = index
        self.text = entries[index] if index < len(entries) else ""

    # -- completion ---------------------------------------------------------------

    def _changed(self, change: Change) -> None:
        if not change.text_changed:
            return
        self.on_change.run(self.text)
        self.refresh_candidates()

    def refresh_candidates(self) -> None:
        if self.completer is None or self.closed:
            return
        if self._completion is not None:
            self._completion.cancel()  # a newer change makes the older question moot
        loop = self.frame.editor.loop
        if loop is None:
            return
        self._completion = loop.create_task(self._complete())
        self.frame.editor.keep_task(self._completion)

    async def _complete(self) -> None:
        completer = self.completer
        assert completer is not None
        text = self.text
        try:
            found = await completer.candidates(text, self.view.cursor.pos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.frame.editor.report_exception("completing", exc)
            found = []
        if self.closed or self.text != text:
            return
        self.candidates = list(found)[:MAX_CANDIDATES]
        self.selected = 0
        self.frame.popup_changed()

    def select(self, delta: int) -> None:
        if self.candidates:
            self.selected = (self.selected + delta) % len(self.candidates)
            self.frame.popup_changed()

    def accept_candidate(self) -> bool:
        """Put the highlighted candidate in the minibuffer.  False if there is none."""
        if not self.candidates:
            return False
        self.text = self.candidates[min(self.selected, len(self.candidates) - 1)].text
        return True


@provisional("0.1")
class MinibufferAPI:
    """`ed.minibuffer`."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor

    @provisional("0.1")
    async def read(
        self,
        label: str,
        *,
        completer: Completer | None = None,
        history: str | None = None,
        initial: str = "",
        multiline: bool = False,
        masked: bool = False,
        submit_selects: bool = False,
    ) -> str:
        """Ask the user for text in the frame's prompt area.

        Gives up the command lane while waiting, so the keys that answer can
        run their own commands.  Raises Quit if the prompt is cancelled.
        """
        frame = self.editor.frame
        if frame is None:
            raise NoFrame("nothing is attached to ask")
        minibuffer = Minibuffer(
            frame,
            label,
            multiline=multiline,
            masked=masked,
            completer=completer,
            history=history,
            initial=initial,
            submit_selects=submit_selects,
        )
        frame.prompt(minibuffer)
        minibuffer.refresh_candidates()
        answer = minibuffer.answer()
        try:
            return await self.editor.dispatcher.wait(answer)
        finally:
            with contextlib.suppress(Exception):
                minibuffer.close()
            if answer.done() and not answer.cancelled():
                answer.exception()  # seen: nothing is left to warn about
