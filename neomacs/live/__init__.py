"""Live evaluation (ENGINE 9)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neomacs.live.evaluate import NO_VALUE, Evaluation, evaluate
from neomacs.stability import provisional

__all__ = ["NO_VALUE", "Evaluation", "evaluate", "forget", "keep"]


@provisional("0.1")
def keep[T](key: str, factory: Callable[[], T]) -> T:
    """State that survives re-evaluation (ENGINE 9.4): created by FACTORY the
    first time KEY is asked for, and the same object returned ever after."""
    from neomacs.editor import current

    kept: dict[str, Any] = current().kept
    if key not in kept:
        kept[key] = factory()
    return kept[key]


@provisional("0.1")
def forget(key: str) -> None:
    """Discard what `keep` holds under KEY, so it is created afresh next time."""
    from neomacs.editor import current

    current().kept.pop(key, None)
