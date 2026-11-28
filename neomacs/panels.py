"""Panels and placement (ENGINE 8.5): where a buffer appears when a command shows it."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from neomacs.options import Option
from neomacs.stability import provisional
from neomacs.window import View

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer
    from neomacs.window import Frame

_panel_ids = itertools.count(1)
SIDES = ("left", "right", "top", "bottom")


@provisional("0.1")
@dataclass(frozen=True)
class Main:
    """Show in the main area.  With SPLIT "h" or "v", in a new window beside or below."""

    split: str | None = None


@provisional("0.1")
@dataclass(frozen=True)
class InPanel:
    """Show as a tab of the panel called NAME, creating it docked to a side or
    floating at an anchor if it does not exist yet."""

    name: str
    dock: str | None = None
    floating: Any = None  # "center", an edge or corner name, or "cursor"
    transient: bool = False
    size: float = 0.3


PLACEMENT: dict[str, Main | InPanel] = {
    "document": Main(),
    "help": InPanel("side", dock="right", size=0.4),
    "traceback": InPanel("bottom", dock="bottom"),
    "debug": InPanel("bottom", dock="bottom"),
    "messages": InPanel("bottom", dock="bottom"),
    "search": InPanel("bottom", dock="bottom"),
    "repl": InPanel("bottom", dock="bottom"),
    "output": InPanel("bottom", dock="bottom"),
    "preview": InPanel("preview", dock="bottom", transient=True, size=0.3),
    "peek": InPanel("peek", floating="cursor", transient=True),
}

Option(
    "placement",
    dict,
    dict(PLACEMENT),
    "Where each kind of buffer is shown: a role name to Main(...) or InPanel(...).",
)


class PanelView(View):
    """A view shown as a tab of a panel."""

    panel: Panel


@provisional("0.1")
class Panel:
    """A named container beside or over the main area, holding views as tabs."""

    def __init__(
        self,
        frame: Frame,
        name: str,
        *,
        mode: str = "docked",
        side: str = "bottom",
        anchor: Any = "center",
        transient: bool = False,
        size: float = 0.3,
    ) -> None:
        self.id = next(_panel_ids)
        self.frame = frame
        self.name = name
        self.mode = mode
        self.side = side
        self.anchor = anchor
        self.transient = transient
        self.size = size
        self.visible = True
        self.tabs: list[PanelView] = []
        self.active: PanelView | None = None

    @provisional("0.1")
    def show(self, buffer: Buffer, *, focus: bool = False) -> View:
        """Show BUFFER as the active tab, reusing a tab that already shows it."""
        view = next((tab for tab in self.tabs if tab.buffer is buffer), None)
        if view is None:
            view = PanelView(self.frame, buffer)
            view.panel = self
            self.tabs.append(view)
        typing_here = self.frame.focus in self.tabs and self.frame.focus is not view
        if focus or not typing_here:
            # Output arriving in a panel does not take the place of the tab
            # the user is working in; it becomes a tab beside it.
            self.active = view
        self.visible = True
        if focus:
            self.frame.focus_view(view)
        self.frame.schedule_update()
        return view

    @provisional("0.1")
    def close(self, view: View) -> None:
        """Close a tab.  Its buffer survives.  A panel with no tabs left is hidden."""
        if view not in self.tabs:
            return
        index = self.tabs.index(view)  # pyright: ignore[reportArgumentType]
        had_focus = self.frame.focus is view
        self.tabs.pop(index)
        if self.active is view:
            self.active = self.tabs[min(index, len(self.tabs) - 1)] if self.tabs else None
        view.close()
        if not self.tabs:
            self.visible = False
        if had_focus:
            self.frame.focus_view(self.active if self.active is not None else self.frame.window)
        self.frame.schedule_update()

    @provisional("0.1")
    def activate(self, view: View) -> None:
        if view in self.tabs:
            self.active = view  # pyright: ignore[reportAttributeAccessIssue]
            self.frame.schedule_update()

    @provisional("0.1")
    def dock(self, side: str) -> None:
        if side not in SIDES:
            raise ValueError(f"not a side: {side}")
        self.mode, self.side, self.visible = "docked", side, True
        self.frame.schedule_update()

    @provisional("0.1")
    def float(self, anchor: Any = "center") -> None:
        self.mode, self.anchor, self.visible = "floating", anchor, True
        self.frame.schedule_update()

    @provisional("0.1")
    def hide(self) -> None:
        """Take the panel off the screen, keeping its tabs."""
        self.visible = False
        if self.frame.focus in self.tabs:
            self.frame.focus_view(self.frame.window)
        self.frame.schedule_update()

    @provisional("0.1")
    def reveal(self) -> None:
        if self.tabs:
            self.visible = True
            self.frame.schedule_update()

    def describe(self) -> dict[str, Any]:
        """The panel as the `layout` event carries it (ENGINE 4.3)."""
        described: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "visible": self.visible and bool(self.tabs),
            "mode": self.mode,
            "size": self.size,
            "tabs": [{"view": tab.id, "title": tab.buffer.name} for tab in self.tabs],
            "active": self.active.id if self.active is not None else None,
        }
        if self.mode == "docked":
            described["side"] = self.side
        else:
            anchor = self.anchor
            if anchor == "cursor":
                view = self.frame.window
                line, offset = view.buffer.line_offset(view.cursor.pos)
                anchor = {"view": view.id, "line": line, "offset": offset}
            described["anchor"] = anchor
            if self.transient:
                described["transient"] = True
        return described

    def __repr__(self) -> str:
        return f"<Panel {self.name} {self.mode} {len(self.tabs)} tabs>"


def place(frame: Frame, buffer: Buffer, role: str, *, focus: bool) -> View:
    """Show BUFFER in FRAME where ROLE belongs."""
    table: dict[str, Any] = dict(frame.editor.options.placement)
    where = table.get(role) or table.get("document") or Main()
    if not frame.draws_panels:
        where = Main()  # a frontend with one window: everything is shown in it
    if isinstance(where, InPanel):
        panel = frame.panels.get(where.name)
        if panel is None:
            # The settings are for creating it; later the user's changes are kept.
            panel = frame.panels[where.name] = Panel(
                frame,
                where.name,
                mode="floating" if where.floating is not None else "docked",
                side=where.dock or "bottom",
                anchor=where.floating or "center",
                transient=where.transient,
                size=where.size,
            )
        return panel.show(buffer, focus=focus)
    split = where.split if isinstance(where, Main) else None
    target: View | None = next((w for w in frame.windows() if w.buffer is buffer), None)
    if target is None:
        target = frame.split(frame.window, split, focus=False) if split else frame.window
        if role != "document" and target.buffer is not buffer:
            buffer.locals["_shown_over"] = target.buffer.name  # for quit_window
        target.show(buffer)
    if focus:
        frame.focus_view(target)
    return target
