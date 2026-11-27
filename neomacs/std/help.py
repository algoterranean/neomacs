"""Standard commands: finding out what things do (ENGINE 9.6)."""

from __future__ import annotations

import annotationlib
import inspect
import re
from collections.abc import Callable
from typing import Annotated, Any

from neomacs import commands as commands_module
from neomacs import keys as keys_module
from neomacs import options as options_module
from neomacs.commands import Ask, Command, command
from neomacs.completion import Candidate, FunctionCompleter, rank
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.keymap import Keymap, global_map
from neomacs.live import run as live
from neomacs.text.buffer import Buffer


class HelpMode(live.SpecialMode):
    name = "help"
    keymap = Keymap("help")


def _location(fn: Any) -> tuple[str, int] | None:
    """The file (or buffer) and line where a function is defined."""
    fn = inspect.unwrap(fn)
    code = getattr(fn, "__code__", None)
    if code is None:
        return None
    return code.co_filename, code.co_firstlineno


def _help(write: Callable[[Buffer], None]) -> None:
    buffer = live.special_buffer("*help*", HelpMode)
    write(buffer)
    live.finish_special(buffer)
    shown = ed.window.buffer
    if shown is not buffer:
        buffer.locals["_shown_over"] = shown.name
    view = ed.show(buffer, "help", focus=True)
    view.selections.set([(0, 0)])
    view.reveal(0, "top")


def _link(buffer: Buffer, text: str, **props: Any) -> None:
    start = len(buffer)
    buffer.insert(start, text)
    buffer.layer("links").add(start, len(buffer), face="underline", **props)


def _heading(buffer: Buffer, text: str) -> None:
    start = len(buffer)
    buffer.insert(start, text + "\n")
    buffer.layer("faces").add(start, len(buffer) - 1, face="bold")


def _keys_for(name: str) -> list[str]:
    return [
        keys_module.describe(list(sequence))
        for sequence, bound in global_map.bindings()
        if bound == name
    ]


def _describe_command(buffer: Buffer, found: Command) -> None:
    _heading(buffer, found.name)
    fn = inspect.unwrap(found.fn)
    try:
        signature = str(inspect.signature(fn, annotation_format=annotationlib.Format.STRING))
    except Exception:
        signature = "(…)"
    buffer.insert(len(buffer), f"\n{fn.__name__}{signature}\n\n")
    buffer.insert(len(buffer), (found.doc or "Not documented.") + "\n\n")
    bound = _keys_for(found.name)
    buffer.insert(len(buffer), f"Keys: {', '.join(bound) if bound else 'none'}\n")
    buffer.insert(len(buffer), f"Defined in {found.owner}")
    where = _location(found.fn)
    if where is not None:
        buffer.insert(len(buffer), ", ")
        _link(buffer, f"{where[0]}, line {where[1]}", jump=where)
    buffer.insert(len(buffer), "\n")


def _need_command(name: str) -> Command:
    found = commands_module.lookup(name)
    if found is None:
        raise NeomacsError(f"No command called {name}")
    return found


@command
def describe_command(name: Annotated[str, Ask.command("Describe command: ")]) -> None:
    """Show what a command does, its keys, and where it is defined."""
    found = _need_command(name)
    _help(lambda buffer: _describe_command(buffer, found))


@command
async def describe_key() -> None:
    """Show what a key sequence runs.  Type the keys when asked."""
    frame = ed.frame
    if frame is None:
        raise NeomacsError("No frame")
    maps = ed.dispatcher.active_keymaps(frame, ed.view)
    pressed: list[str] = []
    ed.message("Describe key: ", kind="keys")
    while True:
        pressed.append(await ed.read_key())
        binding: str | Keymap | None = None
        for keymap in maps:
            binding = keymap.lookup(pressed)
            if binding is not None:
                break
        if binding is None and len(pressed) == 1 and keys_module.text_of(pressed[0]) is not None:
            binding = next((m.default for m in maps if m.default), None)
        described = keys_module.describe(pressed)
        if isinstance(binding, Keymap):
            ed.message(f"Describe key: {described}-", kind="keys")
            continue
        if binding is None:
            ed.message(f"{described} is undefined")
            return
        found = commands_module.lookup(binding)
        if found is None:
            ed.message(f"{described} runs {binding}, which is not defined")
            return

        def write(buffer: Buffer, keys: str = described, found: Command = found) -> None:
            buffer.insert(len(buffer), f"{keys} runs the command ")
            _describe_command(buffer, found)

        _help(write)
        return


def _option_names(text: str) -> list[Candidate]:
    return rank(
        (Candidate(o.name, o.doc.split("\n", 1)[0]) for o in options_module.options.values()), text
    )


@command
async def describe_option() -> None:
    """Show an option's meaning, its value here, and where that value comes from."""
    name = await ed.minibuffer.read(
        "Describe option: ", completer=FunctionCompleter(_option_names), history="options"
    )
    option = options_module.options.get(options_module.resolve(name))
    if option is None:
        raise NeomacsError(f"No option called {name}")
    origin, value = ed.buffer.options.origin(option.name)

    def write(buffer: Buffer) -> None:
        _heading(buffer, option.name)
        kinds = option.type if isinstance(option.type, tuple) else (option.type,)
        buffer.insert(
            len(buffer),
            f"\n{option.doc or 'Not documented.'}\n\n"
            f"Type: {' or '.join(k.__name__ for k in kinds)}\n"
            f"Value here: {value!r}  (from {origin})\n"
            f"Default: {option.default!r}\n",
        )

    _help(write)


@command
def find_source(name: Annotated[str, Ask.command("Find source of command: ")]) -> None:
    """Go to the definition of a command, in its file or in the buffer it was evaluated from."""
    where = _location(_need_command(name).fn)
    if where is None:
        raise NeomacsError(f"{name} has no Python source")
    live.open_location(*where)


@command
def apropos(pattern: Annotated[str, Ask.string("Apropos (regexp): ", history="apropos")]) -> None:
    """List the commands and options whose name or summary matches a regular expression."""
    try:
        wanted = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise NeomacsError(f"Bad regular expression: {exc}") from None
    found = sorted(
        (c for c in commands_module.commands.values() if wanted.search(f"{c.name} {c.summary}")),
        key=lambda c: c.name,
    )
    chosen = sorted(
        (o for o in options_module.options.values() if wanted.search(f"{o.name} {o.doc}")),
        key=lambda o: o.name,
    )
    if not found and not chosen:
        raise NeomacsError(f"Nothing matches {pattern}")

    def write(buffer: Buffer) -> None:
        if found:
            _heading(buffer, "Commands")
        for item in found:
            where = _location(item.fn)
            if where is not None:
                _link(buffer, item.name, jump=where)
            else:
                buffer.insert(len(buffer), item.name)
            bound = _keys_for(item.name)
            keys = f"  ({', '.join(bound[:2])})" if bound else ""
            buffer.insert(len(buffer), f"{keys}  {item.summary}\n")
        if chosen:
            _heading(buffer, ("\n" if found else "") + "Options")
        for option in chosen:
            buffer.insert(len(buffer), f"{option.name}  {option.doc.split(chr(10), 1)[0]}\n")

    _help(write)
