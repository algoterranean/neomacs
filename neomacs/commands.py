"""Commands (ENGINE 7.1): plain functions registered by name, with declared parameters."""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from neomacs.ownership import ownership
from neomacs.stability import provisional


@provisional("0.1")
class Prefix:
    """Annotation: the parameter takes the numeric prefix argument, or DEFAULT."""

    def __init__(self, default: int = 1) -> None:
        self.default = default


class _Marker:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


# Annotation: the parameter takes the whole PrefixArg, or None.
RawPrefix = _Marker("RawPrefix")
# Annotation: the parameter takes the text of the key that invoked the command.
KeyText = _Marker("KeyText")
# Annotation: the parameter takes the key that invoked the command, in key notation.
KeyName = _Marker("KeyName")


@provisional("0.1")
@dataclass(frozen=True)
class PrefixArg:
    value: int  # 4 per bare universal argument (4, 16, ...), -1 for a bare minus
    explicit: bool  # digits were typed
    universal: int  # how many bare universal arguments were typed
    negative: bool = False

    def describe(self) -> str:
        if self.explicit:
            return f"C-u {self.value}-"
        if self.universal:
            return " ".join(["C-u"] * self.universal) + (" -" if self.negative else "") + "-"
        return "M---"


@provisional("0.1")
@dataclass(frozen=True)
class Ask:
    """Annotation: the parameter is asked for in a minibuffer when not supplied."""

    kind: str
    prompt: str
    history: str | None = None
    choices: tuple[str, ...] = ()
    initial: str = ""

    @staticmethod
    def string(prompt: str, *, history: str | None = None, initial: str = "") -> Ask:
        return Ask("string", prompt, history, (), initial)

    @staticmethod
    def file(prompt: str) -> Ask:
        return Ask("file", prompt, "files")

    @staticmethod
    def buffer(prompt: str) -> Ask:
        return Ask("buffer", prompt, "buffers")

    @staticmethod
    def command(prompt: str) -> Ask:
        return Ask("command", prompt, "commands")

    @staticmethod
    def choice(prompt: str, choices: list[str] | tuple[str, ...]) -> Ask:
        return Ask("choice", prompt, None, tuple(choices))

    @staticmethod
    def integer(prompt: str) -> Ask:
        return Ask("integer", prompt)


@dataclass
class Param:
    name: str
    spec: Any = None  # a Prefix, RawPrefix, KeyText or Ask; None for a plain parameter
    is_selection: bool = False
    required: bool = True
    convert: Any = None  # the annotated type, for converting what was asked for


@provisional("0.1")
@dataclass
class Command:
    name: str
    fn: Callable[..., Any]
    doc: str = ""
    params: list[Param] = field(default_factory=list[Param])
    background: bool = False
    prefix: bool = False  # a prefix-argument command: does not consume the pending prefix
    amalgamate: bool = False  # consecutive invocations undo as one step, as typing does
    owner: str = ""

    @property
    def per_selection(self) -> bool:
        return any(p.is_selection for p in self.params)

    @property
    def summary(self) -> str:
        return self.doc.strip().split("\n", 1)[0] if self.doc else ""


commands: dict[str, Command] = {}
# Former names of commands (STABILITY 4): old name -> new name.
aliases: dict[str, str] = {}
_alias_warned: set[str] = set()


def _params(fn: Callable[..., Any]) -> list[Param]:
    from neomacs.selection import Selection

    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    params: list[Param] = []
    for name, parameter in inspect.signature(fn).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        hint = hints.get(name, parameter.annotation)
        param = Param(name, required=parameter.default is inspect.Parameter.empty)
        if typing.get_origin(hint) is typing.Annotated:
            base, *metadata = typing.get_args(hint)
            param.convert = base
            for meta in metadata:
                if isinstance(meta, (Prefix, Ask)) or meta in (RawPrefix, KeyText, KeyName):
                    param.spec = meta
            hint = base
        if hint is Selection or hint == "Selection":
            param.is_selection = True
        params.append(param)
    return params


@typing.overload
def command[F: Callable[..., Any]](fn: F, /) -> F: ...
@typing.overload
def command[F: Callable[..., Any]](
    *,
    name: str | None = None,
    background: bool = False,
    prefix: bool = False,
    amalgamate: bool = False,
) -> Callable[[F], F]: ...
@provisional("0.1")
def command(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    background: bool = False,
    prefix: bool = False,
    amalgamate: bool = False,
) -> Any:
    """Register a function as a command.  The function itself is returned unchanged.

    The default name is the function's.  Registering an existing name replaces
    the function, which is how a redefinition takes effect.
    """

    def register(function: Callable[..., Any]) -> Callable[..., Any]:
        command_name = name or function.__name__
        owner = getattr(function, "__module__", "?") or "?"
        commands[command_name] = Command(
            command_name,
            function,
            inspect.getdoc(function) or "",
            _params(function),
            background=background,
            prefix=prefix,
            amalgamate=amalgamate,
            owner=owner,
        )
        ownership.record(owner, "command", command_name, lambda: commands.pop(command_name, None))
        return function

    return register(fn) if fn is not None else register


@provisional("0.1")
def alias(old: str, new: str) -> None:
    """OLD is a former name of the command NEW.  It keeps working, with one warning."""
    aliases[old] = new


@provisional("0.1")
def lookup(name: str) -> Command | None:
    """The command called NAME, following a rename if there was one."""
    found = commands.get(name)
    if found is None and name in aliases:
        if name not in _alias_warned:
            _alias_warned.add(name)
            from neomacs.stability import _warn  # pyright: ignore[reportPrivateUsage]

            _warn(f"the command {name} has been renamed to {aliases[name]}")
        found = commands.get(aliases[name])
    return found
