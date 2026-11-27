"""Reading small files that something outside the editor supplies.

A project's `.git/HEAD` or `.neomacs/settings.toml` is read on the editor's
main loop, when a file in that project is opened.  Whatever is at that path
must not be able to stall the editor.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_regular(path: Path, limit: int) -> bytes:
    """The first LIMIT bytes of the regular file at PATH.

    Anything else there (a named pipe with nothing writing to it, say) raises
    OSError rather than being waited on.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        return os.read(fd, limit)
    finally:
        os.close(fd)
