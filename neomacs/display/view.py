"""The view builder: one buffer line as attributes and styled runs (ENGINE 4.3, 8.3)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from neomacs.display.faces import FaceTable

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer

# A decoration provider computes faces on demand:
#   provider(buffer, start, end) -> iterable of (start, end, face name, priority)
Decoration = tuple[int, int, str, int]
Provider = Callable[["Buffer", int, int], Iterable[Decoration]]

Line = list[Any]  # [attrs, runs]


def build_line(buffer: Buffer, line: int, faces: FaceTable, providers: Iterable[Provider]) -> Line:
    """Build 1-based LINE of BUFFER: ``[attrs, runs]``.

    Each run is ``[offset, text, face_id]``; offset is in code points from the
    start of the line, or None for virtual text that is not in the buffer.
    """
    start = buffer.line_start(line)
    end = buffer.line_end(line)
    text = buffer.store.slice(start, end)
    attrs: dict[str, Any] = {}
    gutter: dict[str, tuple[int, list[Any]]] = {}

    # Everything that styles or alters this line, as (start, end, priority, props).
    pieces: list[tuple[int, int, int, dict[str, Any]]] = []
    for provider in providers:
        for d_start, d_end, face, priority in provider(buffer, start, end):
            if d_start < end and d_end > start:
                pieces.append((max(d_start, start), min(d_end, end), priority, {"face": face}))
    for layer in buffer.layers:
        for span in layer.overlapping(start, end):
            props = span.props
            line_props = props.get("line")
            if line_props and span.start >= start:
                attrs.update(line_props)
            mark = props.get("gutter")
            if mark and start <= span.start <= end:
                column = mark.get("column", "signs")
                priority = mark.get("priority", span.priority)
                if column not in gutter or priority >= gutter[column][0]:
                    face_id = faces.id_for(mark["face"]) if mark.get("face") else 0
                    gutter[column] = (priority, [mark.get("text", ""), face_id])
            pieces.append((max(span.start, start), min(span.end, end), span.priority, props))
    if gutter:
        attrs["gutter"] = {column: mark for column, (_, mark) in gutter.items()}

    if not pieces:
        return [attrs, [[0, text, 0]] if text else []]

    # Cut the line wherever something starts or ends, then style each segment.
    cuts = {start, end}
    for p_start, p_end, _, _ in pieces:
        cuts.add(p_start)
        cuts.add(p_end)
    ordered = sorted(cuts)
    runs: list[list[Any]] = []

    def emit(offset: int | None, run_text: str, face_id: int) -> None:
        if not run_text:
            return
        last = runs[-1] if runs else None
        if (
            last is not None
            and len(last) == 3
            and last[2] == face_id
            and offset is not None
            and last[0] is not None
            and last[0] + len(last[1]) == offset
        ):
            last[1] += run_text
        else:
            runs.append([offset, run_text, face_id])

    def virtual(at: int, key: str) -> None:
        for p_start, p_end, _, props in pieces:
            value = props.get(key)
            if value and (p_start if key == "before_text" else p_end) == at:
                emit(None, value, faces.id_for(props["face"]) if props.get("face") else 0)

    for i, seg_start in enumerate(ordered):
        virtual(seg_start, "after_text")
        virtual(seg_start, "before_text")
        for p_start, p_end, _, props in pieces:
            # An inline object stands in place of the text its span covers.
            if props.get("display") and p_start == seg_start:
                runs.append([seg_start - start, {**props["display"], "covers": p_end - p_start}])
        if i + 1 == len(ordered):
            break
        seg_end = ordered[i + 1]
        active = [p for p in pieces if p[0] <= seg_start and p[1] >= seg_end and p[0] < p[1]]
        if any(props.get("invisible") or props.get("display") for _, _, _, props in active):
            continue
        active.sort(key=lambda p: p[2])
        names = tuple(props["face"] for _, _, _, props in active if props.get("face"))
        emit(seg_start - start, text[seg_start - start : seg_end - start], faces.id_for(names))
    return [attrs, runs]
