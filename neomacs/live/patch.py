"""Redefinition in place (ENGINE 9.3).

When a name that held a function or class is defined again, the old object
is updated to behave like the new one and stays bound to the name.  Whatever
held a reference to it (a hook, a timer, a `from` import, an instance) then
runs the new code.  Where that cannot be done the name is simply rebound, and
the caller is told why so it can warn.
"""

from __future__ import annotations

import contextlib
import weakref
from types import FunctionType
from typing import Any

# Every function or class object that has been bound to a module-level name:
# (module name, name) -> weak references.  All of them are patched on a
# redefinition, not only the one the name currently holds, because a decorator
# may have registered an earlier one somewhere.
_known: dict[tuple[str, str], list[weakref.ref[Any]]] = {}

_CLASS_SKIP = frozenset({"__dict__", "__weakref__"})

# A class or function that was discarded in favour of the patched original:
# new object -> the object that was kept.  A subclass defined in the same
# evaluation names the new base; it has to be recognised as the old one.
_replaced: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def kept(obj: Any) -> Any:
    """The object that stands for OBJ after patching: itself, or the original it replaced."""
    try:
        return _replaced.get(obj, obj)
    except TypeError:
        return obj


def _functions_in(value: Any) -> list[FunctionType]:
    """The plain functions behind a class attribute."""
    if isinstance(value, FunctionType):
        return [value]
    if isinstance(value, (classmethod, staticmethod)):
        return _functions_in(value.__func__)  # pyright: ignore[reportUnknownMemberType]
    if isinstance(value, property):
        return [f for part in (value.fget, value.fset, value.fdel) for f in _functions_in(part)]
    return []


def _point_class_cell(new: type, old: type) -> None:
    """Methods that use `super()` or `__class__` find the class in a cell that
    every method of one class body shares.  Make the new body's cell hold the
    class that is being kept, so methods moved onto it work there."""
    for value in vars(new).values():
        for fn in _functions_in(value):
            if "__class__" in fn.__code__.co_freevars and fn.__closure__ is not None:
                cell = fn.__closure__[fn.__code__.co_freevars.index("__class__")]
                with contextlib.suppress(ValueError):
                    if cell.cell_contents is new:
                        cell.cell_contents = old


def remember(module: str, name: str, obj: Any) -> None:
    refs = _known.setdefault((module, name), [])
    refs[:] = [r for r in refs if r() is not None and r() is not obj]
    with contextlib.suppress(TypeError):
        refs.append(weakref.ref(obj))


def known(module: str, name: str) -> list[Any]:
    return [o for o in (r() for r in _known.get((module, name), [])) if o is not None]


def patch_function(old: FunctionType, new: FunctionType) -> str | None:
    """Make OLD behave as NEW.  Returns None, or the reason it could not be done."""
    if old.__code__.co_freevars != new.__code__.co_freevars:
        return "the variables it closes over changed"
    try:
        old.__code__ = new.__code__
    except ValueError as exc:
        return str(exc)
    old.__defaults__ = new.__defaults__
    old.__kwdefaults__ = new.__kwdefaults__
    old.__doc__ = new.__doc__
    old.__qualname__ = new.__qualname__
    old.__type_params__ = new.__type_params__
    annotate = getattr(new, "__annotate__", None)
    if annotate is not None:
        with contextlib.suppress(AttributeError, TypeError):
            old.__annotate__ = annotate  # pyright: ignore[reportFunctionMemberAccess]
    else:
        with contextlib.suppress(Exception):
            old.__annotations__ = dict(new.__annotations__)
    old.__dict__.clear()
    old.__dict__.update(new.__dict__)
    return None


def _patch_callable(old: Any, new: Any) -> str | None:
    """Patch a function, following `__wrapped__` so decorated functions work too."""
    if not (isinstance(old, FunctionType) and isinstance(new, FunctionType)):
        return "a decorator returned something that is not a plain function"
    inner_old = getattr(old, "__wrapped__", None)
    inner_new = getattr(new, "__wrapped__", None)
    reason = patch_function(old, new)
    if reason is not None:
        return reason
    if inner_old is not None and inner_new is not None:
        reason = _patch_callable(inner_old, inner_new)
        if reason is None:
            old.__wrapped__ = inner_old  # pyright: ignore[reportFunctionMemberAccess]
        return reason
    if (inner_old is None) != (inner_new is None):
        return "it gained or lost a decorator"
    return None


def patch_class(old: type, new: type) -> str | None:
    """Make OLD behave as NEW, so existing instances and subclasses follow."""
    if type(old) is not type(new):
        return "its metaclass changed"
    if type(old) is not type:
        return "it has a custom metaclass"
    if len(old.__bases__) != len(new.__bases__) or any(
        a is not kept(b) for a, b in zip(old.__bases__, new.__bases__, strict=True)
    ):
        return "its base classes changed"
    if vars(old).get("__slots__") != vars(new).get("__slots__"):
        return "its __slots__ changed"
    _point_class_cell(new, old)
    for attr, value in list(vars(new).items()):
        if attr in _CLASS_SKIP:
            continue
        current = vars(old).get(attr)
        if isinstance(current, FunctionType) and isinstance(value, FunctionType):
            if patch_function(current, value) is None:
                continue
        elif isinstance(current, type) and isinstance(value, type):
            if patch_class(current, value) is None:
                continue
        with contextlib.suppress(AttributeError, TypeError):
            setattr(old, attr, value)
    for attr in list(vars(old)):
        if attr not in vars(new) and attr not in _CLASS_SKIP:
            with contextlib.suppress(AttributeError, TypeError):
                delattr(old, attr)
    return None


def _patch(old: Any, new: Any) -> str | None:
    if isinstance(old, type) and isinstance(new, type):
        return patch_class(old, new)
    if isinstance(old, type) or isinstance(new, type):
        return "it changed between a class and a function"
    return _patch_callable(old, new)


def redefine(module: str, name: str, old: Any, new: Any) -> tuple[Any, str | None]:
    """A name that held OLD now holds NEW.  Returns what the name should be
    bound to, and a reason if existing references could not be updated."""
    if old is new or old is None:
        remember(module, name, new)
        return new, None
    if not isinstance(old, (FunctionType, type)):
        remember(module, name, new)
        return new, None  # it was an ordinary value: nothing to keep alive
    if not isinstance(new, (FunctionType, type)):
        return new, "it is no longer a function or class"
    earlier = [o for o in known(module, name) if o is not old and o is not new]
    reason = _patch(old, new)
    if reason is not None:
        remember(module, name, new)
        return new, reason
    for other in earlier:
        _patch(other, new)  # best effort: older generations someone may still hold
    remember(module, name, old)
    remember(module, name, new)
    with contextlib.suppress(TypeError):
        _replaced[new] = old
    return old, None
