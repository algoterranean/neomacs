"""Stability markers for the public API (STABILITY 3.1).

Every name is stable, provisional or internal.  The markers record the tier
and do nothing else at run time; `neomacs.tools.manifest` collects them.
"""

from __future__ import annotations

import functools
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


def deprecated[T](message: str, *, since: str, removal: str) -> Callable[[T], T]:
    """Mark a function as deprecated (STABILITY 3.5).

    Each calling module gets one warning, in `*messages*`.  The deprecation is
    recorded on the function for the manifest and for `neomacs ext check`.
    """

    def mark(obj: T) -> T:
        fn: Any = obj
        if not callable(fn):
            raise TypeError("only functions can be marked deprecated")
        warned: set[str] = set()

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            import sys

            caller = str(sys._getframe(1).f_globals.get("__name__", "?"))  # pyright: ignore[reportPrivateUsage]
            if caller not in warned:
                warned.add(caller)
                _warn(
                    f"{caller} uses {fn.__module__}.{fn.__qualname__}, deprecated since "
                    f"{since} and to be removed in {removal}: {message}"
                )
            return fn(*args, **kwargs)

        existing = getattr(fn, ATTR, None)
        if existing is not None:
            setattr(wrapper, ATTR, dict(existing))
        marked: Any = wrapper
        marked.__neomacs_deprecated__ = {"message": message, "since": since, "removal": removal}
        return cast(T, wrapper)

    return mark


def _warn(text: str) -> None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    if editor is not None:
        editor.message(text, kind="warning")
    else:
        import warnings

        warnings.warn(text, DeprecationWarning, stacklevel=3)


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
