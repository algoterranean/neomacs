"""The owner thread: the one thread allowed to touch editor state (ENGINE D1).

Normally this is the thread running the engine's event loop.  The debugger
hands ownership to its own loop while the main thread is paused.  When no
owner has been claimed (plain library use, most unit tests) any thread may
proceed.
"""

from __future__ import annotations

import os
import threading

_owner: int | None = None
checking: bool = os.environ.get("NEOMACS_CHECK_OWNER", "") not in ("", "0")


def claim() -> None:
    """Make the calling thread the owner."""
    global _owner
    _owner = threading.get_ident()


def release() -> None:
    global _owner
    _owner = None


def owner() -> int | None:
    return _owner


def check() -> None:
    """Raise if checking is on and the caller is not the owner thread."""
    if checking and _owner is not None and threading.get_ident() != _owner:
        raise RuntimeError(
            "editor state touched from a thread that does not own it "
            f"(owner {_owner}, caller {threading.get_ident()})"
        )
