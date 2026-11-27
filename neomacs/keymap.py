"""Keymaps (ENGINE 7.2): tries from keys to command names or further keymaps."""

from __future__ import annotations

from collections.abc import Iterator

from neomacs import keys as keys_module
from neomacs.ownership import caller_module, ownership
from neomacs.stability import provisional

Binding = "str | Keymap"


@provisional("0.1")
class Keymap:
    """Bindings store command names, not functions, so redefining a command
    needs no rebinding."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._bindings: dict[str, str | Keymap] = {}
        # The command for a text key nothing else binds (the global map uses
        # this for self-insertion).
        self.default: str | None = None

    @provisional("0.1")
    def bind(self, sequence: str, command: str | Keymap) -> None:
        """Bind a key sequence such as "C-x C-f" to a command name or a keymap."""
        keys = keys_module.parse(sequence)
        owner = caller_module()
        keymap = self
        for key in keys[:-1]:
            inner = keymap._bindings.get(key)
            if not isinstance(inner, Keymap):
                inner = Keymap(f"{keymap.name} {key}".strip())
                keymap._bindings[key] = inner
            keymap = inner
        keymap._bindings[keys[-1]] = command
        target, last = keymap, keys[-1]
        ownership.record(owner, "key", (id(self), keys), lambda: target._bindings.pop(last, None))

    @provisional("0.1")
    def unbind(self, sequence: str) -> None:
        keys = keys_module.parse(sequence)
        keymap: Keymap = self
        for key in keys[:-1]:
            inner = keymap._bindings.get(key)
            if not isinstance(inner, Keymap):
                return
            keymap = inner
        keymap._bindings.pop(keys[-1], None)
        ownership.forget("key", (id(self), keys))

    @provisional("0.1")
    def lookup(self, keys: tuple[str, ...] | list[str]) -> str | Keymap | None:
        """What KEYS is bound to: a command name, a keymap if it is a prefix, or None."""
        found: str | Keymap | None = self
        for key in keys:
            if not isinstance(found, Keymap):
                return None
            found = found._bindings.get(key)
        return found

    @provisional("0.1")
    def bindings(self, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
        """Every (key sequence, command name) in this keymap."""
        for key, value in self._bindings.items():
            if isinstance(value, Keymap):
                yield from value.bindings((*prefix, key))
            else:
                yield (*prefix, key), value

    @provisional("0.1")
    def where_is(self, command: str) -> tuple[str, ...] | None:
        """A key sequence bound to COMMAND here, the shortest if there are several."""
        found = [keys for keys, name in self.bindings() if name == command]
        return min(found, key=lambda k: (len(k), len(" ".join(k)), k)) if found else None

    def __repr__(self) -> str:
        return f"<Keymap {self.name or 'anonymous'} ({len(self._bindings)} keys)>"


global_map = Keymap("global")
