"""The registry of RPC methods, and the `engine.*` methods (ENGINE 4.2)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from neomacs.errors import RpcError

if TYPE_CHECKING:
    from neomacs.rpc.protocol import Connection

# The protocol version (STABILITY 5).  Engine and frontend must agree exactly.
PROTOCOL = 1
CAPABILITIES: list[str] = []

_methods: dict[str, Callable[..., Any]] = {}


class _NoReply:
    def __repr__(self) -> str:
        return "NO_REPLY"


# Returned by a handler whose request has been answered by other means.
NO_REPLY = _NoReply()


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


async def _eval(conn: Connection, source: str, mode: str) -> str | None:
    from neomacs.live import run
    from neomacs.live.evaluate import NO_VALUE, evaluate

    scratch = conn.editor.scratch
    result = await evaluate(source, scratch.__dict__, filename="<engine.eval>", mode=mode)
    run.announce(scratch.__name__, result)
    return None if result.value is NO_VALUE else repr(result.value)


@method("engine.eval")
def engine_eval(conn: Connection, source: str, mode: str = "auto") -> Any:
    """Evaluate Python in the scratch module; return the repr of the value, if any.

    Top-level `await` is allowed.  Code that does not await runs to completion
    before the next message is read.
    """
    task = asyncio.Task(_eval(conn, source, mode), eager_start=True)
    if task.done():
        return task.result()
    conn.editor.keep_task(task)
    return task


@method("engine.call")
async def engine_call(conn: Connection, command: str, args: list[Any] | None = None) -> Any:
    """Run a named command in this connection's frame, or the most recent frame."""
    from neomacs import commands
    from neomacs.window import Frame

    editor = conn.editor
    if commands.lookup(command) is None:
        raise RpcError(f"no such command: {command}")
    frame = conn.frame if isinstance(conn.frame, Frame) else editor.frame
    if frame is None:
        raise RpcError("no frame to run a command in")
    result: Any = await editor.dispatcher.call(frame, command, args or [])
    if isinstance(result, (str, int, float, bool, list, dict)):
        return cast(Any, result)
    return None


@method("engine.exit")
def engine_exit(conn: Connection) -> None:
    """Ask the engine to exit."""
    conn.editor.stop()
