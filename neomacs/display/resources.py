"""Resources (ENGINE 4.3): images and other binary things a line can refer to by id."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from neomacs.stability import provisional


@provisional("0.1")
class Resources:
    """The editor's table of resources.  A frame is sent the definition of
    each one the first time a line it is sent uses it."""

    def __init__(self) -> None:
        self._items: dict[int, tuple[str, dict[str, Any]]] = {}
        self._by_path: dict[str, int] = {}
        self._next = 1

    @provisional("0.1")
    def image(
        self, path: Path | str | None = None, *, data: bytes | None = None, mime: str | None = None
    ) -> int:
        """Register an image, by file or by its bytes, and return its id.
        The same file registered again gives the same id."""
        if path is not None:
            resolved = str(Path(path).expanduser().resolve())
            found = self._by_path.get(resolved)
            if found is not None:
                return found
            source: dict[str, Any] = {"path": resolved}
        elif data is not None:
            source = {
                "mime": mime or "application/octet-stream",
                "data": base64.b64encode(data).decode("ascii"),
            }
        else:
            raise ValueError("an image needs a path or data")
        number = self._next
        self._next += 1
        self._items[number] = ("image", source)
        if path is not None:
            self._by_path[source["path"]] = number
        return number

    def describe(self, number: int, *, local: bool = True) -> tuple[str, dict[str, Any]] | None:
        """(kind, source) as sent to a frontend.  One that cannot read the
        engine's files is sent the bytes instead of the path."""
        item = self._items.get(number)
        if item is None:
            return None
        kind, source = item
        if "path" in source and not local:
            path = Path(source["path"])
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                return None
            return kind, {"mime": mime, "data": data}
        return kind, dict(source)

    def __len__(self) -> int:
        return len(self._items)
