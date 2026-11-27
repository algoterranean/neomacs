"""Standard commands and options: how the editor looks."""

from __future__ import annotations

from typing import Any

from neomacs.commands import command
from neomacs.completion import Candidate, FunctionCompleter, rank
from neomacs.display import faces
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.options import Option


def _apply(value: Any, buffer: Any) -> None:
    if buffer is None and value in faces.themes and ed.faces.theme != value:
        ed.faces.load_theme(value)


theme = Option(
    "theme",
    str,
    faces.DEFAULT_THEME,
    "The theme: what colours the named faces have. `dark` and `light` come with the editor.",
    validate=lambda name: name in faces.themes,
)
theme.on_change.add(_apply)


@command(name="theme.select")
async def select_theme() -> None:
    """Choose a theme.  Every window changes at once."""
    names = [Candidate(name) for name in sorted(faces.themes)]
    name = await ed.minibuffer.read(
        "Theme: ", completer=FunctionCompleter(lambda text: rank(names, text)), history="themes"
    )
    if name not in faces.themes:
        raise NeomacsError(f"There is no theme called {name}")
    ed.options.theme = name
    ed.message(f"Theme: {name}")
