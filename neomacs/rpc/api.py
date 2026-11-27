"""The registry of RPC methods, and the `engine.*` methods (ENGINE 4.2)."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from neomacs.errors import RpcError

if TYPE_CHECKING:
    from neomacs.rpc.protocol import Connection

# The protocol version (STABILITY 5).  Engine and frontend must agree exactly.
PROTOCOL = 1
CAPABILITIES: list[str] = []

_methods: dict[str, Callable[..., Any]] = {}


def method[F: Callable[..., Any]](name: str) -> Callable[[F], F]:
    """Register a handler.  It is called as ``handler(connection, *params)``."""

    def register(fn: F) -> F:
        _methods[name] = fn
        return fn

    return register


def lookup(name: str) -> Callable[..., Any] | None:
    return _methods.get(name)


def _describe(name: str, fn: Callable[..., Any]) -> dict[str, Any]:
    params = list(inspect.signature(fn).parameters.values())[1:]  # drop the connection
    return {
        "name": name,
        "params": [
            {"name": p.name, "optional": p.default is not inspect.Parameter.empty} for p in params
        ],
        "doc": inspect.getdoc(fn) or "",
    }


@method("engine.api_info")
def api_info(conn: Connection) -> dict[str, Any]:
    """Protocol version, capabilities, and the list of methods."""
    import neomacs

    return {
        "version": neomacs.__version__,
        "protocol": PROTOCOL,
        "capabilities": list(CAPABILITIES),
        "methods": [_describe(name, fn) for name, fn in sorted(_methods.items())],
    }


@method("engine.eval")
def engine_eval(conn: Connection, source: str, mode: str = "auto") -> str | None:
    """Evaluate Python in the scratch module; return the repr of the value, if any."""
    from neomacs.live.evaluate import NO_VALUE, evaluate

    value = evaluate(source, conn.editor.scratch.__dict__, filename="<engine.eval>", mode=mode)
    return None if value is NO_VALUE else repr(value)


@method("engine.call")
def engine_call(conn: Connection, command: str, args: list[Any] | None = None) -> Any:
    """Run a named command."""
    raise RpcError(f"no such command: {command}")


@method("engine.exit")
def engine_exit(conn: Connection) -> None:
    """Ask the engine to exit."""
    conn.editor.stop()
