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

# Faces that say what text is, not how a theme colours it.  Under every theme.
BASE: dict[str, Attrs] = {
    "heading-1": {"size": 1.6, "weight": "bold"},
    "bold": {"weight": "bold"},
    "italic": {"slant": "italic"},
    "underline": {"underline": True},
}

DARK: dict[str, Attrs] = {
    "default": {"fg": "#d4d4d4", "bg": "#1e1e1e"},
    "modeline": {"fg": "#1e1e1e", "bg": "#a8a8a8"},
    "selection": {"bg": "#264f78"},
    "cursor": {"fg": "#1e1e1e", "bg": "#d4d4d4"},
    "gutter": {"fg": "#6e7681"},
    "placeholder": {"fg": "#4b5563"},
    "keyword": {"fg": "#c586c0"},
    "string": {"fg": "#ce9178"},
    "comment": {"fg": "#6a9955", "slant": "italic"},
    "function": {"fg": "#dcdcaa"},
    "type": {"fg": "#4ec9b0"},
    "number": {"fg": "#b5cea8"},
    "constant": {"fg": "#569cd6"},
    "decorator": {"fg": "#c8a35f"},
    "match": {"bg": "#613214"},
    "match-current": {"fg": "#1e1e1e", "bg": "#f8c555"},
    "bracket-match": {"bg": "#3f4f5f", "weight": "bold"},
    "tab-active": {"fg": "#ffffff", "bg": "#3c3c3c", "weight": "bold"},
    "tab": {"fg": "#a0a0a0", "bg": "#2a2a2a"},
    "border": {"fg": "#5a5a5a"},
    "error": {"fg": "#f44747"},
    "breakpoint": {"fg": "#f44747"},
    "debug-line": {"bg": "#3a3d16"},
    "warning": {"fg": "#cca700"},
    # What a graphical frontend draws around the text: lists and boxes that
    # float over it, notices, the lines between things, what is picked out.
    "popup": {"fg": "#d4d4d4", "bg": "#252526"},
    "popup-selected": {"bg": "#094771"},
    "toast": {"bg": "#3c3c3c"},
    "chrome-border": {"fg": "#454545"},
    "divider": {"fg": "#333333"},
    "accent": {"fg": "#0e639c"},
    "scrollbar": {"fg": "#4a4a4a"},
}

LIGHT: dict[str, Attrs] = {
    "default": {"fg": "#1f2328", "bg": "#ffffff"},
    "modeline": {"fg": "#1f2328", "bg": "#d0d7de"},
    "selection": {"bg": "#b6d7ff"},
    "cursor": {"fg": "#ffffff", "bg": "#1f2328"},
    "gutter": {"fg": "#8c959f"},
    "placeholder": {"fg": "#afb8c1"},
    "keyword": {"fg": "#a626a4"},
    "string": {"fg": "#0a7d33"},
    "comment": {"fg": "#6e7781", "slant": "italic"},
    "function": {"fg": "#6f42c1"},
    "type": {"fg": "#0e7490"},
    "number": {"fg": "#0550ae"},
    "constant": {"fg": "#0550ae"},
    "decorator": {"fg": "#953800"},
    "match": {"bg": "#fff3b0"},
    "match-current": {"fg": "#1f2328", "bg": "#ffd33d"},
    "bracket-match": {"bg": "#d0d7de", "weight": "bold"},
    "tab-active": {"fg": "#1f2328", "bg": "#ffffff", "weight": "bold"},
    "tab": {"fg": "#57606a", "bg": "#eaeef2"},
    "border": {"fg": "#afb8c1"},
    "error": {"fg": "#cf222e"},
    "breakpoint": {"fg": "#cf222e"},
    "debug-line": {"bg": "#fff8c5"},
    "warning": {"fg": "#9a6700"},
    "popup": {"fg": "#1f2328", "bg": "#f6f8fa"},
    "popup-selected": {"bg": "#b6d7ff"},
    "toast": {"bg": "#eaeef2"},
    "chrome-border": {"fg": "#d0d7de"},
    "divider": {"fg": "#d8dee4"},
    "accent": {"fg": "#0969da"},
    "scrollbar": {"fg": "#c1c7cd"},
}

# Every theme, by name.  Evaluating this module again must not lose the ones
# other modules defined.
themes: dict[str, dict[str, Attrs]] = globals().get("themes", {})
themes.setdefault("dark", DARK)
themes.setdefault("light", LIGHT)
DEFAULT_THEME = "dark"

# Faces the frontend draws with on its own account, sent as `ui_faces`.
UI_FACES = (
    "selection", "cursor", "gutter", "placeholder", "modeline",
    "popup", "popup-selected", "toast", "chrome-border", "divider", "accent", "scrollbar",
    "error", "warning",
)  # fmt: skip


@provisional("0.1")
def define_theme(name: str, faces: dict[str, Attrs], *, base: str = "dark") -> None:
    """Define the theme NAME: BASE's faces, with FACES over them.

    A theme gives every named face its attributes.  Defining an existing name
    again replaces it; an editor showing that theme takes it up when the
    theme is next selected.
    """
    if base not in themes:
        raise KeyError(f"no theme called {base}")
    merged = {face: dict(attrs) for face, attrs in themes[base].items()}
    for face, attrs in faces.items():
        unknown = set(attrs) - ATTRIBUTES
        if unknown:
            raise ValueError(f"unknown face attributes in {face}: {sorted(unknown)}")
        merged[face] = dict(attrs)
    themes[name] = merged
    from neomacs.ownership import caller_module, ownership

    ownership.record(caller_module(), "theme", name, lambda: themes.pop(name, None))


@provisional("0.1")
class FaceTable:
    """Named faces, and the ids of the combinations of them that are in use.

    Id 0 is the default face.  Every other id holds only the attributes its
    faces set; the frontend lets the rest come from face 0.
    """

    def __init__(self) -> None:
        self.theme = DEFAULT_THEME
        # Faces defined with `define`.  They stay when the theme changes.
        self.own: dict[str, Attrs] = {}
        self.named: dict[str, Attrs] = {}
        self._compose()
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
        self.own[name] = dict(attrs)
        self.named[name] = dict(attrs)
        self._changed()

    def _compose(self) -> None:
        chosen = themes.get(self.theme) or themes[DEFAULT_THEME]
        self.named = {name: dict(attrs) for name, attrs in (BASE | chosen | self.own).items()}

    def _changed(self) -> None:
        self.generation += 1
        for callback in list(self.on_change):
            callback()

    @provisional("0.1")
    def load_theme(self, name: str) -> None:
        """Give every named face the attributes the theme NAME has for it.
        Faces defined with `define` keep what they were given.  Frames are
        sent the new attributes; no text is sent again."""
        if name not in themes:
            raise KeyError(f"no theme called {name}")
        self.theme = name
        self._compose()
        self._changed()

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
