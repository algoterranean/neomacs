"""Options (ENGINE 6): typed, documented, optionally buffer-local settings."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from neomacs.hooks import Hook
from neomacs.ownership import caller_module, ownership
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

options: dict[str, Option] = {}
# Old name -> new name, for options that were renamed (STABILITY 4).
aliases: dict[str, str] = {}


@provisional("0.1")
class Option:
    """A declared setting.  Declaring an existing name again updates the
    declaration and keeps the values already set."""

    def __init__(
        self,
        name: str,
        type: type | tuple[type, ...],
        default: Any,
        doc: str = "",
        *,
        validate: Callable[[Any], bool] | None = None,
        project: bool = False,
    ) -> None:
        self.name = name
        self.type = type
        self.doc = doc
        self.validate = validate
        self.project = project
        previous = options.get(name)
        self.on_change: Hook = (
            previous.on_change if previous is not None else Hook(f"option-changed:{name}")
        )
        self.default = self.check(default)
        options[name] = self
        ownership.record(caller_module(), "option", name, lambda: options.pop(name, None))

    @provisional("0.1")
    def check(self, value: Any) -> Any:
        """VALUE if it is acceptable; otherwise raise TypeError or ValueError."""
        types = self.type if isinstance(self.type, tuple) else (self.type,)
        ok = isinstance(value, types)
        if isinstance(value, bool) and bool not in types:
            ok = False  # True is an int to Python, but not to a setting
        if not ok and float in types and isinstance(value, int) and not isinstance(value, bool):
            value, ok = float(value), True
        if not ok:
            names = " or ".join(t.__name__ for t in types)
            raise TypeError(f"option {self.name} must be {names}, not {value!r}")
        if self.validate is not None and not self.validate(value):
            raise ValueError(f"{value!r} is not a valid value for option {self.name}")
        return value

    def __repr__(self) -> str:
        return f"<Option {self.name}>"


def resolve(name: str) -> str:
    """NAME, or what it was renamed to."""
    return aliases.get(name, name)


@provisional("0.1")
def alias(old: str, new: str) -> None:
    """OLD is a former name of the option NEW.  Values set under OLD apply to NEW."""
    aliases[old] = new


class OptionStore:
    """The global values of one editor, and values set before their option was declared."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, name: str) -> tuple[bool, Any]:
        if name not in self.values:
            return False, None
        value = self.values[name]
        option = options.get(name)
        if option is not None:
            try:
                return True, option.check(value)
            except TypeError, ValueError:
                return False, None  # set before the declaration, and not acceptable to it
        return True, value


_fallback_store = OptionStore()  # for buffers used with no editor at all


def _store() -> OptionStore:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    return editor.option_store if editor is not None else _fallback_store


def _mode_default(buffer: Buffer, name: str) -> tuple[bool, Any]:
    mode = getattr(buffer, "mode", None)
    for cls in type(mode).__mro__ if mode is not None else ():
        defaults = vars(cls).get("defaults")
        if isinstance(defaults, dict) and name in defaults:
            return True, defaults[name]  # pyright: ignore[reportUnknownVariableType]
    return False, None


@provisional("0.1")
class OptionView:
    """`ed.options` (global values) or `buf.options` (one buffer's view of them).

    Reading looks in the buffer, then its project, then its mode's defaults,
    then the global value, then the option's default.  Assigning sets the
    value at this view's own level.
    """

    __slots__ = ("_buffer",)

    def __init__(self, buffer: Buffer | None = None) -> None:
        object.__setattr__(self, "_buffer", buffer)

    @provisional("0.1")
    def origin(self, name: str) -> tuple[str, Any]:
        """(where the value comes from, the value)."""
        name = resolve(name)
        buffer: Buffer | None = self._buffer
        option = options.get(name)
        if buffer is not None:
            local: dict[str, Any] = buffer.option_values
            project: dict[str, Any] = buffer.project_values
            if name in local:
                return "buffer", cast(Any, local[name])
            if option is not None and option.project and name in project:
                return "project", cast(Any, project[name])
            found, value = _mode_default(buffer, name)
            if found:
                return "mode", value
        found, value = _store().get(name)
        if found:
            return "global", value
        if option is None:
            raise AttributeError(f"no option named {name}")
        return "default", option.default

    def __getattr__(self, name: str) -> Any:
        return self.origin(name)[1]

    def __setattr__(self, name: str, value: Any) -> None:
        name = resolve(name)
        option = options.get(name)
        buffer: Buffer | None = self._buffer
        if option is not None:
            value = option.check(value)
        elif buffer is not None:
            raise AttributeError(f"no option named {name}")
        # With no declaration yet, a global value is held until there is one.
        if buffer is not None:
            buffer.option_values[name] = value
        else:
            _store().values[name] = value
        if option is not None:
            option.on_change.run(value, buffer, buffer=buffer)
        if buffer is not None:
            buffer.invalidate(0, len(buffer))

    @provisional("0.1")
    def unset(self, name: str) -> None:
        """Remove the value set at this level."""
        name = resolve(name)
        buffer: Buffer | None = self._buffer
        (buffer.option_values if buffer is not None else _store().values).pop(name, None)

    @provisional("0.1")
    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self.origin(name)[1]
        except AttributeError:
            return default


# -- options the core itself uses ---------------------------------------------

Option("tab_width", int, 8, "Width of a tab stop, in columns.", validate=lambda n: 0 < n <= 64,
       project=True)  # fmt: skip
Option("indent_with_tabs", bool, False, "Indent with tab characters.", project=True)
Option("fill_column", int, 80, "Column beyond which lines are wrapped when filling.",
       validate=lambda n: n > 0, project=True)  # fmt: skip
Option("wrap", bool, True, "Wrap long lines instead of truncating them.")
Option("line_numbers", str, "absolute", "Line numbers in the gutter: off, absolute or relative.",
       validate=lambda v: v in ("off", "absolute", "relative"))  # fmt: skip
Option("gutter_columns", list, ["numbers"], "Gutter columns, in order, by id.")
Option("key_hint_delay", float, 0.6, "Seconds before a pending key prefix is shown.")
Option("typing_undo_limit", int, 20, "Consecutive typed characters undone as one step.")
