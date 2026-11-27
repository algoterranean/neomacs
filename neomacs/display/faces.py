"""Faces: named sets of text attributes, resolved to small integer ids (ENGINE 8.3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neomacs.stability import provisional

Attrs = dict[str, Any]

# Attribute names a face may have (ENGINE 4.3).
ATTRIBUTES = frozenset(
    {"family", "size", "weight", "slant", "fg", "bg", "underline", "strike", "box", "line_height"}
)

DEFAULT_THEME: dict[str, Attrs] = {
    "default": {"fg": "#d4d4d4", "bg": "#1e1e1e"},
    "modeline": {"fg": "#1e1e1e", "bg": "#a8a8a8"},
    "selection": {"bg": "#264f78"},
    "cursor": {"fg": "#1e1e1e", "bg": "#d4d4d4"},
    "gutter": {"fg": "#6e7681"},
    "placeholder": {"fg": "#4b5563"},
    "keyword": {"fg": "#c586c0"},
    "string": {"fg": "#ce9178"},
    "comment": {"fg": "#6a9955", "slant": "italic"},
    "heading-1": {"size": 1.6, "weight": "bold"},
    "bold": {"weight": "bold"},
    "italic": {"slant": "italic"},
    "underline": {"underline": True},
    "error": {"fg": "#f44747"},
    "warning": {"fg": "#cca700"},
}

# Faces the frontend draws with on its own account, sent as `ui_faces`.
UI_FACES = ("selection", "cursor", "gutter", "placeholder", "modeline")


@provisional("0.1")
class FaceTable:
    """Named faces, and the ids of the combinations of them that are in use.

    Id 0 is the default face.  Every other id holds only the attributes its
    faces set; the frontend lets the rest come from face 0.
    """

    def __init__(self) -> None:
        self.named: dict[str, Attrs] = {name: dict(attrs) for name, attrs in DEFAULT_THEME.items()}
        self.generation = 0
        self.on_change: list[Callable[[], None]] = []
        self._ids: dict[tuple[str, ...], int] = {("default",): 0}
        self._names: list[tuple[str, ...]] = [("default",)]

    @provisional("0.1")
    def define(self, name: str, **attrs: Any) -> None:
        """Define or redefine the face NAME.  Frames are sent the new attributes."""
        unknown = set(attrs) - ATTRIBUTES
        if unknown:
            raise ValueError(f"unknown face attributes: {sorted(unknown)}")
        self.named[name] = dict(attrs)
        self.generation += 1
        for callback in list(self.on_change):
            callback()

    @provisional("0.1")
    def id_for(self, names: tuple[str, ...] | str) -> int:
        """The id for NAMES merged in order, later names winning."""
        key = (names,) if isinstance(names, str) else tuple(names)
        if not key:
            return 0
        found = self._ids.get(key)
        if found is None:
            found = self._ids[key] = len(self._names)
            self._names.append(key)
        return found

    @provisional("0.1")
    def attrs(self, face_id: int) -> Attrs:
        merged: Attrs = {}
        for name in self._names[face_id]:
            merged.update(self.named.get(name, {}))
        return merged

    def __len__(self) -> int:
        return len(self._names)
