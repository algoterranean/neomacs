"""The application menu (ENGINE 8.6): data in the engine, drawn by the frontend."""

from __future__ import annotations

from typing import Any

from neomacs import keys as keys_module
from neomacs.commands import commands
from neomacs.keymap import Keymap
from neomacs.ownership import caller_module, ownership
from neomacs.stability import provisional


@provisional("0.1")
class Menu:
    def __init__(self) -> None:
        self._items: dict[str, tuple[str, str | None]] = {}  # path -> (command, placed after)
        self.generation = 0

    @provisional("0.1")
    def add(self, path: str, command: str, *, after: str | None = None) -> None:
        """Add an item such as "File/Open…" that runs COMMAND."""
        self._items[path] = (command, after)
        self.generation += 1
        ownership.record(caller_module(), "menu", path, lambda: self.remove(path))

    @provisional("0.1")
    def remove(self, path: str) -> None:
        if self._items.pop(path, None) is not None:
            self.generation += 1

    def tree(self, keymaps: list[Keymap]) -> list[Any]:
        """The menu as nested items, each with its bound key and whether it can run."""
        order = list(self._items)
        for path, (_, after) in self._items.items():
            if after in order:
                order.remove(path)
                order.insert(order.index(after) + 1, path)
        root: list[Any] = []
        for path in order:
            command = self._items[path][0]
            level: list[Any] = root
            *parents, label = path.split("/")
            for parent in parents:
                found: Any = next(
                    (i for i in level if i["label"] == parent and "children" in i), None
                )
                if found is None:
                    found = {"label": parent, "children": []}
                    level.append(found)
                level = found["children"]
            key = next((k for k in (m.where_is(command) for m in keymaps) if k), None)
            item: dict[str, Any] = {
                "label": label,
                "command": command,
                "key": keys_module.describe(key) if key else None,
                "enabled": command in commands,
            }
            level.append(item)
        return root
