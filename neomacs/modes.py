"""Modes (ENGINE 7.3).  The minimum the command system needs; stage 6 completes them."""

from __future__ import annotations

from typing import Any, ClassVar

from neomacs.keymap import Keymap
from neomacs.stability import provisional
from neomacs.syntax.table import DEFAULT, Syntax


@provisional("0.1")
class Mode:
    """A major or minor mode.  Each buffer gets its own instance."""

    name: ClassVar[str] = "fundamental"
    minor: ClassVar[bool] = False
    keymap: ClassVar[Keymap | None] = None
    syntax: ClassVar[Syntax] = DEFAULT
    defaults: ClassVar[dict[str, Any]] = {}

    @classmethod
    def keymaps(cls) -> list[Keymap]:
        """This mode's keymap, then its parent modes', nearest first."""
        found: list[Keymap] = []
        for klass in cls.__mro__:
            keymap = vars(klass).get("keymap")
            if isinstance(keymap, Keymap) and keymap not in found:
                found.append(keymap)
        return found


@provisional("0.1")
class FundamentalMode(Mode):
    """The mode of a buffer that has no other."""

    name = "fundamental"
