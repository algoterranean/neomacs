"""Key notation (ENGINE 4.7): `a`, `C-x`, `M-f`, `<return>`, `S-<left>`, `<space>`."""

from __future__ import annotations

from neomacs.stability import provisional

MODIFIER_ORDER = "CMSs"  # Control, Meta, Shift, Super: the canonical order


@provisional("0.1")
def split(key: str) -> tuple[str, str]:
    """(modifiers in canonical order, base key) of one key."""
    mods: set[str] = set()
    rest = key
    while len(rest) >= 3 and rest[1] == "-" and rest[0] in MODIFIER_ORDER:
        mods.add(rest[0])
        rest = rest[2:]
    return "".join(m for m in MODIFIER_ORDER if m in mods), rest


@provisional("0.1")
def normalize(key: str) -> str:
    """KEY with its modifiers in canonical order.  Raises ValueError if it is not a key."""
    mods, base = split(key)
    if not base or (len(base) > 1 and not (base[0] == "<" and base[-1] == ">" and len(base) > 2)):
        if not _single_cluster(base):
            raise ValueError(f"not a key: {key!r}")
    if base == " ":
        base = "<space>"
    return "".join(f"{m}-" for m in mods) + base


def _single_cluster(text: str) -> bool:
    if not text:
        return False
    if len(text) == 1:
        return True
    import unicodedata

    clusters = list(unicodedata.iter_graphemes(text))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]
    return len(clusters) == 1  # pyright: ignore[reportUnknownArgumentType]


@provisional("0.1")
def parse(sequence: str) -> tuple[str, ...]:
    """A key sequence written with spaces, e.g. "C-x C-f", as a tuple of keys."""
    keys = tuple(normalize(part) for part in sequence.split())
    if not keys:
        raise ValueError("empty key sequence")
    return keys


@provisional("0.1")
def is_printable(key: str) -> bool:
    """True if KEY is text to insert: not a named key and not modified."""
    mods, base = split(key)
    return not mods and not (len(base) > 2 and base[0] == "<" and base[-1] == ">")


@provisional("0.1")
def text_of(key: str) -> str | None:
    """The text KEY would insert, or None if it is not a text key."""
    if key == "<space>":
        return " "
    return key if is_printable(key) else None


@provisional("0.1")
def describe(keys: tuple[str, ...] | list[str]) -> str:
    return " ".join(keys)
