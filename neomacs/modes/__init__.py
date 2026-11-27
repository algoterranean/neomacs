"""Modes (ENGINE 7.3): what kind of text a buffer holds, and what that changes.

A mode is a class.  Each buffer gets its own instance, which is where
per-buffer mode state lives.  A derived mode is a subclass: keymaps, option
defaults and the syntax table are inherited along the class hierarchy.  A
minor mode is the same thing with ``minor = True``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from neomacs.keymap import Keymap
from neomacs.ownership import ownership
from neomacs.stability import provisional
from neomacs.syntax.table import DEFAULT, Syntax

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

# Every mode that has a name, by that name.  Evaluating this module again
# must not empty it: the modes other modules defined would be lost.
modes: dict[str, type[Mode]] = globals().get("modes", {})


# Modes that extensions not yet loaded say they define (EXTENSIONS 6.2): the
# name of each, the file name patterns it is chosen for, and what loads it.
autoload: dict[str, tuple[list[str], Callable[[], None]]] = globals().get("autoload", {})


@provisional("0.1")
def lookup(name: str) -> type[Mode] | None:
    """The mode called NAME.

    When a mode's module is evaluated again its class is updated in place
    (ENGINE 9.3), but the class statement has by then registered the new,
    discarded class.  This gives the one that buffers have instances of.
    """
    from neomacs.live.patch import kept

    found = modes.get(name)
    if found is None and name in autoload:
        # An extension that has not been loaded yet says it defines this mode.
        _, load = autoload.pop(name)
        load()
        found = modes.get(name)
    return kept(found) if found is not None else None


@provisional("0.1")
class Mode:
    """A major or minor mode.  Each buffer gets its own instance."""

    name: ClassVar[str] = ""
    minor: ClassVar[bool] = False
    # A minor mode with scope "global" is on or off for the whole editor.
    scope: ClassVar[str] = "buffer"
    # File name patterns and interpreter names this major mode is chosen for.
    patterns: ClassVar[list[str]] = []
    interpreters: ClassVar[list[str]] = []
    keymap: ClassVar[Keymap | None] = None
    syntax: ClassVar[Syntax] = DEFAULT
    defaults: ClassVar[dict[str, Any]] = {}
    grammar: ClassVar[str | None] = None  # a tree-sitter grammar, if there is one
    # False for modes the editor sets itself, which get no command of their own.
    interactive: ClassVar[bool] = True
    # Whether strings, comments and keywords are given faces (ENGINE 5.9).
    highlight: ClassVar[bool] = False
    keywords: ClassVar[frozenset[str]] = frozenset()
    constants: ClassVar[frozenset[str]] = frozenset()
    # Keywords that introduce a name, and the face that name gets.
    definers: ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = vars(cls).get("name")
        if not isinstance(name, str) or not name:
            return
        modes[name] = cls
        owner = cls.__module__
        ownership.record(owner, "mode", name, lambda: _forget(name, cls))
        if cls.interactive:
            _make_command(cls)

    def __init__(self) -> None:
        self.buffer: Buffer | None = None

    @provisional("0.1")
    def enable(self, buffer: Buffer) -> None:
        """Called when the mode is turned on in BUFFER."""

    @provisional("0.1")
    def disable(self, buffer: Buffer) -> None:
        """Called when the mode is turned off in BUFFER."""

    @classmethod
    def indent_for(cls, buffer: Buffer, line: int) -> int | None:
        """The indentation LINE should have, or None to use the general rule
        (the line before's, adjusted by bracket depth)."""
        return None

    @classmethod
    def keymaps(cls) -> list[Keymap]:
        """This mode's keymap, then its parent modes', nearest first."""
        found: list[Keymap] = []
        for klass in cls.__mro__:
            keymap = vars(klass).get("keymap")
            if isinstance(keymap, Keymap) and keymap not in found:
                found.append(keymap)
        return found

    @classmethod
    def title(cls) -> str:
        return cls.name or cls.__name__

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


def _forget(name: str, cls: type[Mode]) -> None:
    if modes.get(name) is cls:
        del modes[name]


def _command_name(cls: type[Mode]) -> str:
    return cls.name.replace("-", "_") + "_mode"


def _make_command(cls: type[Mode]) -> None:
    """Every named mode gets a command: a major mode's selects it for the
    buffer, a minor mode's turns it on or off."""
    from neomacs import commands

    name = cls.name

    def toggle() -> None:
        from neomacs.editor import current

        ed = current()
        mode = lookup(name)
        if mode is None:
            ed.message(f"There is no mode called {name} any more", kind="error")
            return
        if not mode.minor:
            ed.buffer.set_mode(mode)
            ed.message(f"{mode.title()} mode")
        elif mode.scope == "global":
            on = ed.toggle_global_mode(mode)
            ed.message(f"{mode.title()} mode {'on' if on else 'off'} everywhere")
        else:
            buffer = ed.buffer
            on = buffer.minor(mode) is None
            if on:
                buffer.enable_minor(mode)
            else:
                buffer.disable_minor(mode)
            ed.message(f"{mode.title()} mode {'on' if on else 'off'}")

    summary = (cls.__doc__ or "").strip().split("\n", 1)[0]
    toggle.__name__ = toggle.__qualname__ = _command_name(cls)
    toggle.__module__ = cls.__module__
    toggle.__doc__ = (
        f"Turn {cls.title()} mode on or off." if cls.minor else f"Use {cls.title()} mode here."
    ) + (f"  {summary}" if summary else "")
    commands.command(toggle)


# -- choosing a major mode ---------------------------------------------------------


def _all() -> list[type[Mode]]:
    from neomacs.live.patch import kept

    return [kept(mode) for mode in modes.values()]


_SHEBANG = re.compile(r"^#!\s*(?:\S*/)?(?:env\s+(?:-\S+\s+)*)?([A-Za-z_+-]+)")


@provisional("0.1")
def mode_for(filename: str | None, first_line: str = "") -> type[Mode]:
    """The major mode for a file: by its name, then by its interpreter line."""
    if filename:
        base = filename.rsplit("/", 1)[-1]
        for name, (patterns, load) in list(autoload.items()):
            if any(fnmatch.fnmatchcase(base, pattern) for pattern in patterns):
                autoload.pop(name, None)
                load()  # its mode is now among the others, to be chosen or not
        best: tuple[int, type[Mode]] | None = None
        for mode in _all():
            if mode.minor:
                continue
            for pattern in mode.patterns:
                if fnmatch.fnmatchcase(base, pattern) and (best is None or len(pattern) > best[0]):
                    best = (len(pattern), mode)  # the most specific pattern wins
        if best is not None:
            return best[1]
    found = _SHEBANG.match(first_line)
    if found is not None:
        program = found.group(1).rstrip("0123456789.")
        for mode in _all():
            if not mode.minor and program in mode.interpreters:
                return mode
    return lookup("fundamental") or FundamentalMode


# -- the standard major modes -----------------------------------------------------------


@provisional("0.1")
class FundamentalMode(Mode):
    """The mode of a buffer that has no other."""

    name = "fundamental"


@provisional("0.1")
class TextMode(FundamentalMode):
    """For prose: lines wrap at words, and an apostrophe is part of a word."""

    name = "text"
    patterns = ["*.txt", "*.text", "README", "LICENSE", "COPYING"]
    syntax = Syntax(word_chars="_'’", quotes=(), brackets=(("(", ")"), ("[", "]"), ("{", "}")))
    defaults = {"wrap": True}


@provisional("0.1")
class ProgMode(FundamentalMode):
    """The parent of modes for source code: strings, comments and keywords are shown."""

    highlight = True
    keymap = Keymap("prog")
    defaults = {"indent_with_tabs": False, "auto_indent": True}

    def enable(self, buffer: Buffer) -> None:
        from neomacs.syntax.highlight import install

        install(buffer)
        if buffer.options.get("auto_pair", True):
            from neomacs.std.structure import AutoPairMode

            buffer.enable_minor(AutoPairMode)

    def disable(self, buffer: Buffer) -> None:
        from neomacs.syntax.highlight import uninstall

        uninstall(buffer)


assert ProgMode.keymap is not None
ProgMode.keymap.bind("<tab>", "indent_line")
ProgMode.keymap.bind("<return>", "newline_and_indent")

from neomacs.modes import python as python  # noqa: E402, PLC0414  (registers the mode)
