"""Compare a frontend's answers to `layout.*` with the reference monospace layout.

Gate C of doc/IMPLEMENTATION.md (FRONTEND 12): with a monospaced face and ASCII
text, a frontend that lays out for real must give the answers `monolayout`
gives.  This runs inside the engine, against whichever frontend is attached
to the frame, and returns what it found.

The buffer's wrapped lines must have no spaces or other break opportunities:
a real frontend wraps at word boundaries where it can, and the reference
wraps at the column.  That difference is layout policy, not something the two
are meant to agree on.
"""

from __future__ import annotations

from typing import Any

from neomacs.frontends.model import ViewModel
from neomacs.frontends.monolayout import MonoView
from neomacs.window import Frame


def sample_text() -> str:
    """Lines of several lengths: empty, short with spaces, and long without a break."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lines: list[str] = []
    for n in range(1, 81):
        kind = n % 8
        if kind == 0:
            lines.append("")
        elif kind in (1, 4):
            lines.append(f"short line {n} with spaces")
        elif kind == 2:
            lines.append((alphabet * 12)[: 150 + n])  # wraps once or twice
        elif kind == 3:
            lines.append(f"x{n}")
        elif kind == 5:
            lines.append((alphabet * 20)[: 400 + 3 * n])  # wraps several times
        else:
            lines.append((alphabet * 3)[: 20 + n % 30])
    return "\n".join(lines)


async def compare(
    frame: Frame, cols: int, rows: int, char_width: float, line_height: float, gutter_px: float
) -> dict[str, Any]:
    view = frame.window
    buf = view.buffer
    count = buf.line_count
    model = ViewModel(view.id)
    model.info = {"line_count": count, "wrap": True, "tab_width": 8, "gutter": []}
    texts = [buf.line_text(n) for n in range(1, count + 1)]
    model.splice(1, count, [[{}, [[0, t, 0]] if t else []] for t in texts])
    mono = MonoView(model, cols, rows)

    checked: dict[str, int] = {}
    mismatches: list[dict[str, Any]] = []

    def record(kind: str, question: Any, expected: Any, actual: Any) -> None:
        checked[kind] = checked.get(kind, 0) + 1
        if expected != actual and len(mismatches) < 25:
            mismatches.append(
                {"kind": kind, "asked": question, "reference": expected, "frontend": actual}
            )
        elif expected != actual:
            mismatches.append({"kind": kind})

    # Positions worth asking about: the ends of each line and either side of every wrap.
    positions: list[tuple[int, int]] = []
    for number, text in enumerate(texts, start=1):
        offsets = {0, 1, len(text) // 2, len(text) - 1, len(text)}
        for wrap in range(cols, len(text) + 1, cols):
            offsets.update((wrap - 1, wrap, wrap + 1))
        positions.extend((number, o) for o in sorted(offsets) if 0 <= o <= len(text))

    # layout.move by visual line, each position on its own, for several distances.
    for distance in (1, -1, 2, -2, 5, -5, 11, -11):
        asked = [[line, offset, None] for line, offset in positions]
        reply = await frame.request(
            "layout.move", view.id, asked, "visual_line", distance, timeout=20
        )
        for (line, offset), answer in zip(positions, reply, strict=True):
            expected = mono.move([[line, offset, None]], "visual_line", distance)[0][:2]
            record("move", [line, offset, distance], expected, list(answer[:2]))

    # A walk down and back up, each side carrying its own goal column.
    here_ref: list[Any] = [1, 7, None]
    here_ui: list[Any] = [1, 7, None]
    for step in [1] * 60 + [-1] * 60 + [3] * 15 + [-2] * 20:
        here_ref = mono.move([here_ref], "visual_line", step)[0]
        here_ui = (await frame.request("layout.move", view.id, [here_ui], "visual_line", step))[0]
        record("walk", step, here_ref[:2], list(here_ui[:2]))

    # layout.line_edge.
    for line, offset in positions[:: max(1, len(positions) // 150)]:
        for which in ("start", "end"):
            expected = mono.line_edge(line, offset, which)
            actual = await frame.request("layout.line_edge", view.id, line, offset, which)
            record("line_edge", [line, offset, which], expected, list(actual))

    # layout.viewport and layout.rect, with both scrolled to the top.
    view.reveal(0, "top")
    await frame.request("layout.viewport", view.id)  # lets the reveal take effect
    actual_viewport = await frame.request("layout.viewport", view.id)
    first, last, page = mono.viewport()
    record("viewport.first", None, first, actual_viewport["first"])
    # The reference shows whole rows; a real view may also show part of one more.
    record(
        "viewport.last",
        [last, actual_viewport["last"]],
        True,
        last <= actual_viewport["last"] <= last + 1,
    )
    record("viewport.page", None, page, actual_viewport["page_lines"])
    for line, offset in positions:
        expected_rect = mono.rect(line, offset)
        if expected_rect is None or expected_rect["y"] >= rows - 1:
            continue
        actual_rect = await frame.request("layout.rect", view.id, line, offset)
        # The reference answers in cells and the frontend in pixels, where a row of
        # text sits a pixel or two inside its line box; compare in whole cells.
        cell = [
            round((actual_rect["x"] - gutter_px) / char_width),
            round(actual_rect["y"] / line_height),
        ]
        record("rect", [line, offset], [expected_rect["x"], expected_rect["y"]], cell)

    # layout.move by page, down and back, from the top.
    for distance in (1, 1, -1, -1):
        expected = mono.move([[1, 3, None]], "page", distance)[0][:2]
        actual = (await frame.request("layout.move", view.id, [[1, 3, None]], "page", distance))[0]
        record("page", distance, expected, list(actual[:2]))

    return {
        "cols": cols,
        "rows": rows,
        "lines": count,
        "checked": checked,
        "total": sum(checked.values()),
        "mismatched": len(mismatches),
        "mismatches": mismatches[:25],
    }
