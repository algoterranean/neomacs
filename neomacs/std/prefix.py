"""Standard commands: the prefix argument (ENGINE 7.6)."""

from __future__ import annotations

from typing import Annotated

from neomacs import keys
from neomacs.commands import KeyName, PrefixArg, command
from neomacs.editor import ed
from neomacs.keymap import Keymap

# While an argument is being typed, plain digits, minus and C-u continue it.
argument_map = Keymap("prefix-argument")
for _digit in "0123456789":
    argument_map.bind(_digit, "digit_argument")
argument_map.bind("-", "negative_argument")
argument_map.bind("C-u", "universal_argument")


def _pending(prefix: PrefixArg) -> None:
    frame = ed.frame
    frame.prefix = prefix
    frame.transient = argument_map
    frame.show_keys(prefix.describe())


@command(prefix=True)
def universal_argument() -> None:
    """Begin a prefix argument, or multiply the one being typed by four."""
    current = ed.frame.prefix
    if current is None or current.explicit:
        _pending(PrefixArg(4 if current is None else current.value, current is not None, 1))
    else:
        sign = -1 if current.negative else 1
        _pending(PrefixArg(abs(current.value) * 4 * sign, False, current.universal + 1,
                           current.negative))  # fmt: skip


@command(prefix=True)
def digit_argument(key: Annotated[str, KeyName] = "") -> None:
    """Add a digit to the prefix argument."""
    digit = int(keys.split(key)[1])
    current = ed.frame.prefix
    if current is not None and current.explicit:
        value = current.value * 10 + (-digit if current.value < 0 or current.negative else digit)
        _pending(PrefixArg(value, True, current.universal, current.negative))
    else:
        negative = current is not None and current.negative
        universal = current.universal if current else 0
        _pending(PrefixArg(-digit if negative else digit, True, universal, negative))


@command(prefix=True)
def negative_argument() -> None:
    """Make the prefix argument negative."""
    current = ed.frame.prefix
    if current is None or not current.explicit:
        universal = current.universal if current else 0
        _pending(PrefixArg(-1, False, universal, True))
    else:
        _pending(PrefixArg(-current.value, True, current.universal, not current.negative))
