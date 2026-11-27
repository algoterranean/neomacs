"""Exception types shared across the engine."""

from __future__ import annotations

from typing import Any


class NeomacsError(Exception):
    """Base class for errors raised deliberately by the engine."""


class RpcError(NeomacsError):
    """An error reply to a request, or an error to be sent as one."""

    def __init__(self, message: str, code: int = -32000, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class FrontendGone(NeomacsError):
    """The frontend a request was waiting on has disconnected."""


class NoFrame(NeomacsError):
    """Something needed a frame and none is attached."""


class ReadOnly(NeomacsError):
    """An attempt to change a read-only buffer."""


class Quit(NeomacsError):
    """The user cancelled: a prompt was dismissed, or a command was interrupted."""
