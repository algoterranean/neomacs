"""Grapheme cluster boundaries within a line (ENGINE 5.8)."""

from __future__ import annotations

import unicodedata
from typing import Any

# Added in Python 3.15; the type checker's stubs may not know it yet.
_iter_graphemes: Any = unicodedata.iter_graphemes  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]


def boundaries(line: str) -> list[int] | None:
    """Offsets at which a cluster starts or ends in LINE, including 0 and len(LINE).

    Returns None for ASCII text, where every offset is a boundary.
    """
    if line.isascii():
        return None
    result = [0]
    result.extend(int(g.end) for g in _iter_graphemes(line))
    return result
