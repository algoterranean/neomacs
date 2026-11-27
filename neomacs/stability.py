"""Stability markers for the public API (STABILITY 3.1).

Every name is stable, provisional or internal.  The markers record the tier
and do nothing else at run time; `neomacs.tools.manifest` collects them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

ATTR = "__neomacs_api__"


def _target(obj: Any) -> Any:
    """The object that carries the marker: the function behind a property or method wrapper."""
    if isinstance(obj, property):
        return obj.fget
    if isinstance(obj, (classmethod, staticmethod)):
        return cast(Any, obj).__func__
    return obj


_constants: dict[str, dict[str, str]] = {}


def _marker[T](tier: str, since: str) -> Callable[[T], T]:
    def mark(obj: T) -> T:
        setattr(_target(obj), ATTR, {"tier": tier, "since": since})
        return obj

    return mark


def stable[T](since: str) -> Callable[[T], T]:
    """Mark a function, class, method or property as stable API."""
    return _marker(tier="stable", since=since)


def provisional[T](since: str) -> Callable[[T], T]:
    """Mark a name as provisional API: public, but may still change."""
    return _marker(tier="provisional", since=since)


def constant(qualified_name: str, tier: str, since: str) -> None:
    """Record the tier of a module-level constant, which cannot carry a marker."""
    _constants[qualified_name] = {"tier": tier, "since": since}


def constants() -> dict[str, dict[str, str]]:
    return dict(_constants)


def info(obj: Any) -> dict[str, str] | None:
    """The marker placed directly on OBJ (not inherited), or None."""
    target = _target(obj)
    if isinstance(target, type):
        return vars(target).get(ATTR)
    return getattr(target, "__dict__", {}).get(ATTR)
