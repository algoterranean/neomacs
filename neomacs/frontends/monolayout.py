"""Monospace layout of lines of runs, in character cells.

The reference layout: it wraps, measures wide characters, scrolls, and
answers every `layout.*` request (ENGINE 4.4).  The terminal frontend draws
its result, and the headless test client uses it to say what would be
displayed.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from neomacs.frontends.model import ViewModel

_iter_graphemes: Any = unicodedata.iter_graphemes  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]


def cluster_width(cluster: str) -> int:
    """Terminal columns taken by one grapheme cluster."""
    first = cluster[0]
    if first == "\t":
        return 1  # expanded by the caller
    if "️" in cluster or unicodedata.east_asian_width(first) in ("W", "F"):
        return 2
    if unicodedata.category(first) in ("Mn", "Me", "Cf", "Cc"):
        return 0 if len(cluster) == 1 and first != "‍" else 1
    return 1


@dataclass(slots=True)
class Cell:
    offset: int | None  # code point offset in the line; None for virtual text
    length: int  # code points of buffer text this cell covers
    text: str
    width: int
    face: int


@dataclass(slots=True)
class Row:
    line: int
    index: int  # which row of its line
    cells: list[Cell]
    start: int  # offset of the first buffer character on this row
    end: int  # offset just after the last
    last: bool  # the final row of its line


class MonoView:
    """Layout state for one view at a given size."""

    def __init__(self, model: ViewModel, width: int, height: int) -> None:
        self.model = model
        self.width = width
        self.height = height
        self._cache: dict[int, tuple[int, int, list[Row]]] = {}
        # Lines whose rows were asked for but are not held: the layout guessed.
        self.missing: set[int] = set()

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self._cache.clear()

    # -- gutter -------------------------------------------------------------

    def _gutter_columns(self) -> list[tuple[str, str, int]]:
        columns: list[tuple[str, str, int]] = []
        for column in self.model.info.get("gutter", []):
            if column.get("side", "left") != "left":
                continue
            if column.get("kind") == "numbers":
                width = max(2, len(str(self.model.line_count))) + 1
            else:
                width = int(column.get("width", 1)) + 1
            columns.append((column["id"], column.get("kind", "marks"), width))
        return columns

    @property
    def gutter_width(self) -> int:
        return sum(width for _, _, width in self._gutter_columns())

    @property
    def text_width(self) -> int:
        return max(1, self.width - self.gutter_width)

    def gutter_text(self, row: Row) -> str:
        parts: list[str] = []
        line = self.model.line(row.line)
        marks: dict[str, Any] = line[0].get("gutter", {}) if line is not None else {}
        for column_id, kind, width in self._gutter_columns():
            if row.index != 0:
                parts.append(" " * width)
            elif kind == "numbers":
                parts.append(str(row.line).rjust(width - 1) + " ")
            else:
                mark = marks.get(column_id)
                parts.append((str(mark[0]) if mark else "").ljust(width - 1)[: width - 1] + " ")
        return "".join(parts)

    # -- rows ---------------------------------------------------------------

    def rows(self, number: int) -> list[Row]:
        """The visual rows of line NUMBER.  A line that is not held is one empty row."""
        line = self.model.line(number)
        if line is None:
            if 1 <= number <= self.model.line_count:
                self.missing.add(number)
            return [Row(number, 0, [], 0, 0, True)]
        cached = self._cache.get(number)
        text_width = self.text_width
        if cached is not None and cached[0] == id(line) and cached[1] == text_width:
            return cached[2]
        tab = max(1, int(self.model.info.get("tab_width", 8)))
        wrap = bool(self.model.info.get("wrap", True))
        rows: list[Row] = []
        cells: list[Cell] = []
        col = 0
        start = 0
        position = 0  # buffer offset reached so far
        attrs: dict[str, Any] = line[0]
        # Paragraph attributes (ENGINE 4.3).  A terminal can indent and align;
        # spacing before and after a line needs fractions of a row and is not drawn.
        indent = max(0, min(int(attrs.get("indent", 0)), text_width - 1))
        text_width -= indent
        for run in line[1]:
            if len(run) == 2:  # an inline object: its description, or a placeholder
                offset, face = run[0], 0
                alt = str(run[1].get("alt", "")) if "image" in run[1] else ""
                text = f"[{alt}]" if alt else "\u25af"
                width = sum(cluster_width(text[g.start : g.end]) for g in _iter_graphemes(text))
                covers = int(run[1].get("covers", 0))
                if wrap and cells and col + width > text_width:
                    rows.append(Row(number, len(rows), cells, start, position, False))
                    cells, col, start = [], 0, position
                cells.append(Cell(offset, covers, text, width, face))
                col += width
                position = offset + covers
                continue
            offset, text, face = run
            at = offset
            for g in _iter_graphemes(text):
                cluster = text[g.start : g.end]
                if cluster == "\t":
                    width = tab - (col % tab)
                    shown = " " * width
                else:
                    width = cluster_width(cluster)
                    shown = cluster
                if wrap and cells and col + width > text_width:
                    rows.append(Row(number, len(rows), cells, start, position, False))
                    cells, col, start = [], 0, position
                    if cluster == "\t":
                        width = tab
                        shown = " " * width
                length = len(cluster) if at is not None else 0
                cells.append(Cell(at, length, shown, width, face))
                col += width
                if at is not None:
                    at += len(cluster)
                    position = at
        rows.append(Row(number, len(rows), cells, start, position, True))
        align = attrs.get("align")
        if indent or align in ("center", "right"):
            for row in rows:
                used = sum(cell.width for cell in row.cells)
                slack = max(0, text_width - used)
                pad = indent + (
                    slack if align == "right" else slack // 2 if align == "center" else 0
                )
                if pad:
                    row.cells.insert(0, Cell(None, 0, " " * pad, pad, 0))
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[number] = (id(line), self.text_width, rows)
        return rows

    def _clamp_top(self) -> None:
        m = self.model
        m.top_line = max(1, min(m.top_line, m.line_count))
        m.top_row = max(0, min(m.top_row, len(self.rows(m.top_line)) - 1))

    def _step(self, line: int, row: int, count: int) -> tuple[int, int, int]:
        """Move COUNT rows from (LINE, ROW).  Returns (line, row, rows not moved)."""
        total = self.model.line_count
        while count > 0:
            if row + 1 < len(self.rows(line)):
                row += 1
            elif line < total:
                line, row = line + 1, 0
            else:
                break
            count -= 1
        while count < 0:
            if row > 0:
                row -= 1
            elif line > 1:
                line -= 1
                row = len(self.rows(line)) - 1
            else:
                break
            count += 1
        return line, row, count

    def visible(self) -> list[Row | None]:
        """Exactly `height` entries: the rows on screen, None past the end of the text."""
        self._clamp_top()
        m = self.model
        out: list[Row | None] = []
        line, row = m.top_line, m.top_row
        while len(out) < self.height and line <= m.line_count:
            rows = self.rows(line)
            out.extend(rows[row : row + self.height - len(out)])
            line, row = line + 1, 0
        out.extend([None] * (self.height - len(out)))
        return out

    def viewport(self) -> tuple[int, int, int]:
        rows = [r for r in self.visible() if r is not None]
        first = rows[0].line if rows else 1
        last = rows[-1].line if rows else 1
        return first, last, self.height

    def scroll_rows(self, count: int) -> None:
        self._clamp_top()
        m = self.model
        m.top_line, m.top_row, _ = self._step(m.top_line, m.top_row, count)

    # -- positions ----------------------------------------------------------

    def locate(self, line: int, offset: int) -> tuple[int, int]:
        """(row index within the line, column) of OFFSET in LINE."""
        rows = self.rows(line)
        for row in rows:
            if offset < row.end or row.last:
                col = 0
                for cell in row.cells:
                    if cell.offset is not None and cell.offset >= offset:
                        break
                    col += cell.width
                return row.index, col
        return 0, 0

    def offset_at(self, row: Row, col: float) -> int:
        """The offset of the character at column COL of ROW, or the row's end."""
        x = 0
        last_real: Cell | None = None
        for cell in row.cells:
            if cell.offset is not None:
                if x + cell.width > col:
                    return cell.offset
                last_real = cell
            x += cell.width
        if row.last or last_real is None or last_real.offset is None:
            return row.end
        return last_real.offset  # stay on this row instead of spilling to the next

    def reveal(self, line: int, offset: int, align: str) -> None:
        self._clamp_top()
        m = self.model
        line = max(1, min(line, m.line_count))
        row, _ = self.locate(line, offset)
        if align == "nearest":
            for shown in self.visible():
                if shown is not None and shown.line == line and shown.index == row:
                    return
            if (line, row) < (m.top_line, m.top_row):
                align = "top"
            else:
                align = "bottom"
        back = {"top": 0, "center": self.height // 2, "bottom": self.height - 1}.get(align, 0)
        m.top_line, m.top_row, _ = self._step(line, row, -back)

    def apply_pending_reveal(self) -> None:
        pending = self.model.pending_reveal
        if pending is not None:
            self.model.pending_reveal = None
            self.reveal(*pending)

    def cursor_cell(self, line: int, offset: int) -> tuple[int, int] | None:
        """(screen row, screen column) of a position, or None if it is not on screen."""
        row, col = self.locate(line, offset)
        for y, shown in enumerate(self.visible()):
            if shown is not None and shown.line == line and shown.index == row:
                return y, self.gutter_width + col
        return None

    # -- layout.* requests (ENGINE 4.4) -------------------------------------

    def move(self, positions: list[list[Any]], unit: str, count: int) -> list[list[Any]]:
        if unit == "page":
            count *= max(1, self.height - 1)
            self.scroll_rows(count)
        results: list[list[Any]] = []
        for line, offset, goal in positions:
            line = max(1, min(int(line), self.model.line_count))
            row, col = self.locate(line, int(offset))
            x = col if goal is None else goal
            new_line, new_row, _ = self._step(line, row, count)
            results.append([new_line, self.offset_at(self.rows(new_line)[new_row], x), x])
        return results

    def line_edge(self, line: int, offset: int, which: str) -> list[int]:
        row = self.rows(line)[self.locate(line, offset)[0]]
        if which == "start":
            return [line, row.start]
        return [line, self.offset_at(row, float("inf"))]

    def rect(self, line: int, offset: int) -> dict[str, int] | None:
        cell = self.cursor_cell(line, offset)
        if cell is None:
            return None
        return {"x": cell[1], "y": cell[0], "width": 1, "height": 1}

    # -- for tests and the terminal -----------------------------------------

    def render(self) -> list[str]:
        """The rows on screen as plain text, gutter included."""
        out: list[str] = []
        for row in self.visible():
            if row is None:
                out.append("")
            else:
                out.append((self.gutter_text(row) + "".join(c.text for c in row.cells)).rstrip())
        return out
