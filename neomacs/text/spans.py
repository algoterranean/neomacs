"""Spans: stored ranges that track edits, grouped into named layers (ENGINE 5.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from neomacs.stability import provisional
from neomacs.text.marker import Marker

if TYPE_CHECKING:
    from neomacs.text.buffer import Buffer


@provisional("0.1")
class Span:
    """A range of a buffer with properties.  Text typed at either end stays outside it."""

    __slots__ = ("_end", "_start", "layer", "priority", "props")

    def __init__(
        self, layer: SpanLayer, start: int, end: int, priority: int, props: dict[str, Any]
    ) -> None:
        self.layer = layer
        self._start = Marker(layer.buffer, start, advance=True)
        self._end = Marker(layer.buffer, max(start, end), advance=False)
        self.priority = priority
        self.props = props

    @property
    @provisional("0.1")
    def start(self) -> int:
        return self._start.pos

    @property
    @provisional("0.1")
    def end(self) -> int:
        # After text is deleted around a span its markers can cross; never report that.
        return max(self._start.pos, self._end.pos)

    @provisional("0.1")
    def get(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)

    @provisional("0.1")
    def set(self, **props: Any) -> None:
        """Change properties and redisplay the span."""
        self.props.update(props)
        self.layer.buffer.invalidate(self.start, self.end)

    @provisional("0.1")
    def move(self, start: int, end: int) -> None:
        buf = self.layer.buffer
        buf.invalidate(self.start, self.end)
        self._start.pos = start
        self._end.pos = max(start, end)
        buf.invalidate(self.start, self.end)

    @provisional("0.1")
    def remove(self) -> None:
        self.layer.remove(self)

    def __repr__(self) -> str:
        return f"<Span {self.layer.name} {self.start}..{self.end} {self.props!r}>"


@provisional("0.1")
class SpanLayer:
    """The spans of one kind in one buffer."""

    def __init__(self, buffer: Buffer, name: str) -> None:
        self.buffer = buffer
        self.name = name
        self._spans: list[Span] = []

    @provisional("0.1")
    def add(self, start: int, end: int, *, priority: int = 0, **props: Any) -> Span:
        span = Span(self, start, end, priority, props)
        self._spans.append(span)
        self.buffer.invalidate(span.start, span.end)
        return span

    @provisional("0.1")
    def remove(self, span: Span) -> None:
        if span in self._spans:
            self._spans.remove(span)
            self.buffer.invalidate(span.start, span.end)
            span._start.detach()  # pyright: ignore[reportPrivateUsage]
            span._end.detach()  # pyright: ignore[reportPrivateUsage]

    @provisional("0.1")
    def clear(self) -> None:
        for span in list(self._spans):
            self.remove(span)

    @provisional("0.1")
    def overlapping(self, start: int, end: int) -> list[Span]:
        """Spans that intersect [START, END), plus empty spans inside it, in position order."""
        found = [
            s
            for s in self._spans
            if (s.start < end and s.end > start) or (s.start == s.end and start <= s.start <= end)
        ]
        found.sort(key=lambda s: (s.start, -s.priority))
        return found

    @provisional("0.1")
    def at(self, pos: int) -> list[Span]:
        """Spans containing POS, highest priority first."""
        found = [s for s in self._spans if s.start <= pos < s.end]
        found.sort(key=lambda s: -s.priority)
        return found

    def __iter__(self):  # noqa: ANN204
        return iter(sorted(self._spans, key=lambda s: s.start))

    def __len__(self) -> int:
        return len(self._spans)
