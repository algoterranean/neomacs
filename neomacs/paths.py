"""Where Neomacs keeps things on disk (DISTRIBUTION 5).

This is the one module that knows.  Each directory can be put somewhere else
with an environment variable (`NEOMACS_CONFIG_DIR`, `NEOMACS_DATA_DIR`,
`NEOMACS_CACHE_DIR`), which is how tests keep away from the user's own files.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# sun_path is 104 bytes on macOS and 108 on Linux, including the terminator.
MAX_SOCKET_PATH = 100


def _env_dir(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value) if value else None


def config_dir() -> Path:
    """Configuration: `init.py` and single-file extensions.  Meant to be edited."""
    override = _env_dir("NEOMACS_CONFIG_DIR")
    if override is not None:
        return override
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "neomacs"
    return (_env_dir("XDG_CONFIG_HOME") or Path.home() / ".config") / "neomacs"


def data_dir() -> Path:
    """Data the editor writes and wants kept: the recovery journal."""
    override = _env_dir("NEOMACS_DATA_DIR")
    if override is not None:
        return override
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "neomacs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "neomacs"
    return (_env_dir("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "neomacs"


def cache_dir() -> Path:
    """Data that can be made again."""
    override = _env_dir("NEOMACS_CACHE_DIR")
    if override is not None:
        return override
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "neomacs" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "neomacs"
    return (_env_dir("XDG_CACHE_HOME") or Path.home() / ".cache") / "neomacs"


def init_file() -> Path:
    """The user's init file (EXTENSIONS 3.3)."""
    return config_dir() / "init.py"


def extensions_dir() -> Path:
    """Where single-file extensions are (EXTENSIONS 3.2)."""
    return config_dir() / "extensions"


def journal_dir() -> Path:
    """Where unsaved text is kept in case the engine dies (ENGINE 11.1)."""
    return data_dir() / "journal"


def _private_dir(path: Path) -> Path | None:
    """Create PATH with mode 0700 if possible; None if it cannot be used."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not os.access(path, os.W_OK | os.X_OK):
            return None
    except OSError:
        return None
    return path


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def runtime_dir() -> Path:
    """A short, private, per-user directory for sockets."""
    candidates: list[Path] = []
    xdg = _env_dir("XDG_RUNTIME_DIR")
    if xdg is not None and xdg.is_dir():
        candidates.append(xdg / "neomacs")
    candidates.append(Path(tempfile.gettempdir()) / f"neomacs-{_uid()}")
    candidates.append(Path("/tmp") / f"neomacs-{_uid()}")
    for candidate in candidates:
        # Leave room for "<instance>.sock" after the directory.
        if len(os.fsencode(candidate)) + 24 > MAX_SOCKET_PATH:
            continue
        made = _private_dir(candidate)
        if made is not None:
            return made
    raise OSError("no usable runtime directory for the engine socket")


def socket_path(instance: str | None = None) -> Path:
    """The socket path for an engine instance (default: this process)."""
    name = instance if instance is not None else str(os.getpid())
    path = runtime_dir() / f"{name}.sock"
    check_socket_path(path)
    return path


def check_socket_path(path: Path) -> None:
    if len(os.fsencode(path)) > MAX_SOCKET_PATH:
        raise OSError(f"socket path too long ({len(os.fsencode(path))} bytes): {path}")
