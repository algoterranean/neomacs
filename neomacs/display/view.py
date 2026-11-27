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

    # What starts, ends, or is to be shown at each cut, so that the walk
    # along the line does not look at every piece at every cut: a line of a
    # few thousand characters can have hundreds of pieces.
    starting: dict[int, list[tuple[int, int, int, dict[str, Any]]]] = {}
    ending: dict[int, list[tuple[int, int, int, dict[str, Any]]]] = {}
    virtual_at: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for piece in pieces:
        p_start, p_end, _, props = piece
        if p_start < p_end:
            starting.setdefault(p_start, []).append(piece)
            ending.setdefault(p_end, []).append(piece)
        if props.get("before_text"):
            virtual_at.setdefault((p_start, "before_text"), []).append(props)
        if props.get("after_text"):
            virtual_at.setdefault((p_end, "after_text"), []).append(props)
        if props.get("display"):
            virtual_at.setdefault((p_start, "display"), []).append({**props, "_end": p_end})

    active: list[tuple[int, int, int, dict[str, Any]]] = []
    hidden = 0  # how many active pieces hide their text
    for i, seg_start in enumerate(ordered):
        for piece in ending.get(seg_start, ()):
            active.remove(piece)
            if piece[3].get("invisible") or piece[3].get("display"):
                hidden -= 1
        for key in ("after_text", "before_text"):
            for props in virtual_at.get((seg_start, key), ()):
                emit(None, props[key], faces.id_for(props["face"]) if props.get("face") else 0)
        for props in virtual_at.get((seg_start, "display"), ()):
            # An inline object stands in place of the text its span covers.
            runs.append(
                [seg_start - start, {**props["display"], "covers": props["_end"] - seg_start}]
            )
        if i + 1 == len(ordered):
            break
        for piece in starting.get(seg_start, ()):
            active.append(piece)
            if piece[3].get("invisible") or piece[3].get("display"):
                hidden += 1
        if hidden:
            continue
        seg_end = ordered[i + 1]
        if len(active) > 1:
            active.sort(key=lambda p: p[2])
        names = tuple(props["face"] for _, _, _, props in active if props.get("face"))
        emit(seg_start - start, text[seg_start - start : seg_end - start], faces.id_for(names))
    return [attrs, runs]
