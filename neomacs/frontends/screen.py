"""Where each view goes on a character-cell screen: splits, docked panels with a
tab line, floating panels in a box (ENGINE 8.5).  Shared by the terminal
frontend and the headless test client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

MIN_ROWS = 2  # a window: one row of text and its modeline
MIN_COLS = 8


@dataclass(slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass(slots=True)
class Arrangement:
    # The cells each visible view's text occupies.
    views: dict[int, Rect] = field(default_factory=dict[int, Rect])
    # (view id, x, y, width): where each window's modeline is drawn.
    modelines: list[tuple[int, int, int, int]] = field(
        default_factory=list[tuple[int, int, int, int]]
    )
    # (panel, x, y, width): where each visible panel's tab line is drawn.
    tablines: list[tuple[dict[str, Any], int, int, int]] = field(
        default_factory=list[tuple[dict[str, Any], int, int, int]]
    )
    # (x, y, height): vertical rules between things side by side.
    rules: list[tuple[int, int, int]] = field(default_factory=list[tuple[int, int, int]])
    # Floating panels' boxes, each with the views inside it, drawn last and in this order.
    boxes: list[tuple[Rect, list[int]]] = field(default_factory=list[tuple[Rect, list[int]]])


def _shares(total: int, ratios: list[float], minimum: int) -> list[int]:
    """Divide TOTAL cells in RATIOS, each part at least MINIMUM where there is room."""
    weight = sum(ratios) or 1.0
    sizes = [max(minimum, round(total * r / weight)) for r in ratios]
    # Fix up rounding so the parts add up exactly.  When there is not room
    # for every part at its minimum, the largest give way first, and a part
    # may end with nothing.
    while sum(sizes) < total:
        sizes[sizes.index(min(sizes))] += 1
    while sum(sizes) > total and max(sizes) > 0:
        sizes[sizes.index(max(sizes))] -= 1
    return sizes


def _tree(node: list[Any], rect: Rect, out: Arrangement) -> None:
    if node[0] == "win":
        # With one row the text is shown and the modeline is not; with none
        # the window is not drawn.  The text is never drawn over.
        if rect.h >= MIN_ROWS:
            out.views[node[1]] = Rect(rect.x, rect.y, rect.w, rect.h - 1)
            out.modelines.append((node[1], rect.x, rect.y + rect.h - 1, rect.w))
        elif rect.h == 1 and rect.w > 0:
            out.views[node[1]] = Rect(rect.x, rect.y, rect.w, 1)
        return
    _, _, direction, ratios, children = node
    if direction == "v":
        y = rect.y
        for child, height in zip(children, _shares(rect.h, ratios, MIN_ROWS), strict=True):
            _tree(child, Rect(rect.x, y, rect.w, height), out)
            y += height
    else:
        gaps = len(children) - 1
        x = rect.x
        widths = _shares(max(len(children), rect.w - gaps), ratios, MIN_COLS)
        for index, (child, width) in enumerate(zip(children, widths, strict=True)):
            _tree(child, Rect(x, rect.y, width, rect.h), out)
            x += width
            if index < gaps:
                out.rules.append((x, rect.y, rect.h))
                x += 1


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, max(low, high)))


def arrange(layout: dict[str, Any] | None, cols: int, rows: int, cursor: Any = None) -> Arrangement:
    """Place everything in LAYOUT on a screen of COLS by ROWS cells.

    CURSOR, if given, maps a view id to the (row, column) of its cursor on
    screen, for panels that float beside a position in the text.
    """
    out = Arrangement()
    if layout is None:
        return out
    area = Rect(0, 0, cols, rows)
    floating: list[dict[str, Any]] = []
    for panel in layout.get("panels", []):
        if not panel.get("visible") or panel.get("active") is None:
            continue
        if panel.get("mode") != "docked":
            floating.append(panel)
            continue
        side = panel.get("side", "bottom")
        size = float(panel.get("size", 0.3))
        if side in ("top", "bottom"):
            height = _clamp(round(rows * size), 3, area.h - MIN_ROWS)
            if area.h - height < MIN_ROWS:
                continue  # no room: the panel is not drawn
            y = area.y if side == "top" else area.y + area.h - height
            out.tablines.append((panel, area.x, y, area.w))
            out.views[panel["active"]] = Rect(area.x, y + 1, area.w, height - 1)
            if side == "top":
                area.y += height
            area.h -= height
        else:
            width = _clamp(round(cols * size), 12, area.w - MIN_COLS - 1)
            if area.w - width - 1 < MIN_COLS or area.h < 3:
                continue
            x = area.x if side == "left" else area.x + area.w - width
            out.rules.append((x + width if side == "left" else x - 1, area.y, area.h))
            out.tablines.append((panel, x, area.y, width))
            out.views[panel["active"]] = Rect(x, area.y + 1, width, area.h - 1)
            if side == "left":
                area.x += width + 1
            area.w -= width + 1
    _tree(layout["main"], area, out)
    for panel in floating:
        width = _clamp(round(cols * 0.6), 20, cols - 2)
        height = _clamp(round(rows * float(panel.get("size", 0.3))) + 3, 5, rows - 1)
        x, y = (cols - width) // 2, (rows - height) // 2
        anchor = panel.get("anchor", "center")
        if isinstance(anchor, str):
            if "top" in anchor:
                y = 0
            if "bottom" in anchor:
                y = rows - height
            if "left" in anchor:
                x = 0
            if "right" in anchor:
                x = cols - width
        elif isinstance(anchor, dict) and cursor is not None:
            where = cast(dict[str, Any], anchor)
            at = cursor(where.get("view"), where.get("line"), where.get("offset"))
            if at is not None:
                # Under the position if there is room, otherwise above it.
                y = at[0] + 1 if at[0] + 1 + height <= rows else max(0, at[0] - height)
                x = _clamp(at[1], 0, cols - width)
        box = Rect(max(0, x), max(0, y), width, height)
        out.tablines.append((panel, box.x + 1, box.y + 1, box.w - 2))
        out.views[panel["active"]] = Rect(box.x + 1, box.y + 2, box.w - 2, box.h - 3)
        out.boxes.append((box, [panel["active"]]))
    return out


def tab_line(panel: dict[str, Any], width: int) -> list[tuple[str, bool]]:
    """The tab titles of PANEL as (text, is the active tab), to fill WIDTH cells."""
    parts: list[tuple[str, bool]] = []
    used = 0
    for tab in panel.get("tabs", []):
        text = f" {tab.get('title', '')} "
        if used + len(text) > width:
            text = text[: max(0, width - used)]
        if not text:
            break
        parts.append((text, tab.get("view") == panel.get("active")))
        used += len(text)
    if used < width:
        parts.append((" " * (width - used), False))
    return parts
