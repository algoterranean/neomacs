"""Standard commands: inside a minibuffer."""

from __future__ import annotations

from neomacs.commands import command
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.minibuffer import Minibuffer


def _minibuffer() -> Minibuffer:
    found = Minibuffer.of(ed.view)
    if found is None:
        raise NeomacsError("Not in a minibuffer")
    return found


@command(name="minibuffer.submit")
def submit() -> None:
    """Accept what has been typed."""
    _minibuffer().submit()


@command(name="minibuffer.cancel")
def cancel() -> None:
    """Dismiss the prompt."""
    _minibuffer().cancel()


@command(name="minibuffer.history_previous")
def history_previous() -> None:
    """Replace the input with the previous entry of this prompt's history."""
    _minibuffer().history_step(-1)


@command(name="minibuffer.history_next")
def history_next() -> None:
    """Replace the input with the next entry of this prompt's history."""
    _minibuffer().history_step(1)


@command(name="minibuffer.previous")
def previous() -> None:
    """Highlight the previous candidate, or go back in history if there are none."""
    minibuffer = _minibuffer()
    if minibuffer.candidates:
        minibuffer.select(-1)
    else:
        minibuffer.history_step(-1)


@command(name="minibuffer.next")
def next_() -> None:
    """Highlight the next candidate, or go forward in history if there are none."""
    minibuffer = _minibuffer()
    if minibuffer.candidates:
        minibuffer.select(1)
    else:
        minibuffer.history_step(1)


@command(name="minibuffer.complete")
def complete() -> None:
    """Put the highlighted candidate in the input."""
    if not _minibuffer().accept_candidate():
        ed.message("No completions")
