"""Buffers (ENGINE 5.2), their files (5.6) and grapheme boundaries (5.8)."""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import tempfile
import weakref
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

from neomacs import _owner
from neomacs.errors import ReadOnly
from neomacs.hooks import Hook
from neomacs.stability import provisional
from neomacs.text import graphemes
from neomacs.text.marker import Cursor, Marker
from neomacs.text.spans import SpanLayer
from neomacs.text.store import ChunkedStore, TextStore
from neomacs.text.undo import UndoLog

if TYPE_CHECKING:
    from neomacs.editor import Editor

before_change = Hook("before_change", "Run before a buffer changes, with (buffer, start, end).")
after_change = Hook(
    "after_change", "Run after a buffer changes, with (buffer, start, old_end, new_end)."
)
before_save = Hook("before_save", "Run before a buffer is written to its file, with (buffer).")
after_save = Hook("after_save", "Run after a buffer is written to its file, with (buffer).")
buffer_created = Hook("buffer_created", "Run when a buffer is created, with (buffer).")
buffer_killed = Hook("buffer_killed", "Run when a buffer is killed, with (buffer).")


@dataclass(frozen=True, slots=True)
class Change:
    """One edit, as seen by internal listeners such as views.

    The text in [start, old_end) was replaced by text now in [start, new_end).
    LINE is the line START is on; the edit removed OLD_NEWLINES line breaks and
    inserted NEW_NEWLINES.  For a redisplay request that changed no text,
    old_end == new_end and both newline counts are equal.
    """

    start: int
    old_end: int
    new_end: int
    line: int
    old_newlines: int
    new_newlines: int
    text_changed: bool = True


Listener = Callable[[Change], None]


@provisional("0.1")
class Buffer:
    """Text, with markers, spans, undo and an optional file.

    Positions are 0-based code point offsets and ranges are half open.  Lines
    are numbered from 1.
    """

    def __init__(self, name: str, text: str = "", *, file: Path | None = None) -> None:
        self.name = name
        self.file = file
        self.store: TextStore = ChunkedStore(text)
        self.tick = 0
        self.saved_tick = 0
        self.read_only = False
        self.locals: dict[str, Any] = {}
        self.undo = UndoLog(self)
        # How the text is written back to its file.
        self.encoding = "utf-8"
        self.bom = False
        self.newline = "\n"
        self.decode_fallback = False
        self._markers: weakref.WeakSet[Marker] = weakref.WeakSet()
        self._layers: dict[str, SpanLayer] = {}
        self._listeners: list[Listener] = []
        self._cluster_cache: tuple[int, int, int, list[int] | None] | None = None

    # -- reading ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.store)

    @overload
    def __getitem__(self, index: int) -> str: ...
    @overload
    def __getitem__(self, index: slice) -> str: ...
    def __getitem__(self, index: int | slice) -> str:
        if isinstance(index, slice):
            start, end, step = index.indices(len(self.store))
            if step != 1:
                return self.store.snapshot()[index]
            return self.store.slice(start, max(start, end))
        if index < 0:
            index += len(self.store)
        if not 0 <= index < len(self.store):
            raise IndexError("buffer position out of range")
        return self.store.slice(index, index + 1)

    @property
    @provisional("0.1")
    def text(self) -> str:
        """The whole text."""
        return self.store.snapshot()

    @property
    @provisional("0.1")
    def modified(self) -> bool:
        return self.tick != self.saved_tick

    @provisional("0.1")
    def clamp(self, pos: int) -> int:
        return max(0, min(len(self.store), int(pos)))

    # -- lines --------------------------------------------------------------

    @property
    @provisional("0.1")
    def line_count(self) -> int:
        return self.store.line_count()

    @provisional("0.1")
    def line_start(self, line: int) -> int:
        """Position of the first character of 1-based LINE."""
        return self.store.line_start(line)

    @provisional("0.1")
    def line_end(self, line: int) -> int:
        """Position just after the last character of LINE, before its newline."""
        if line >= self.store.line_count():
            if line > self.store.line_count():
                raise IndexError(f"line {line} out of range")
            return len(self.store)
        return self.store.line_start(line + 1) - 1

    @provisional("0.1")
    def line_of(self, pos: int) -> int:
        return self.store.line_of(pos)

    @provisional("0.1")
    def line_text(self, line: int) -> str:
        """The text of LINE, without its newline."""
        return self.store.slice(self.line_start(line), self.line_end(line))

    @provisional("0.1")
    def position(self, line: int, offset: int = 0) -> int:
        """The position OFFSET code points into LINE, clamped to the line."""
        start = self.line_start(line)
        return max(start, min(start + offset, self.line_end(line)))

    @provisional("0.1")
    def line_offset(self, pos: int) -> tuple[int, int]:
        """(line, offset within line) for POS."""
        line = self.line_of(pos)
        return line, pos - self.line_start(line)

    # -- changing -----------------------------------------------------------

    @provisional("0.1")
    def insert(self, pos: int, text: str) -> None:
        self.replace(pos, pos, text)

    @provisional("0.1")
    def delete(self, start: int, end: int) -> str:
        """Delete [START, END) and return the text removed."""
        removed = self.store.slice(start, end)
        self.replace(start, end, "")
        return removed

    @provisional("0.1")
    def replace(self, start: int, end: int, text: str) -> None:
        """Replace [START, END) with TEXT.  Every change to the text goes through here."""
        _owner.check()
        size = len(self.store)
        if not 0 <= start <= end <= size:
            raise IndexError(f"range {start}..{end} out of bounds 0..{size}")
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        if start == end and not text:
            return
        if self.read_only:
            raise ReadOnly(f"buffer {self.name} is read-only")
        before_change.run(self, start, end, buffer=self)
        removed = self.store.slice(start, end)
        self.undo.record(start, removed, text)
        line = self.store.line_of(start)
        self.store.replace(start, end, text)
        inserted = len(text)
        for marker in tuple(self._markers):
            marker._adjust(start, end, inserted)  # pyright: ignore[reportPrivateUsage]
        self.tick += 1
        self._cluster_cache = None
        change = Change(start, end, start + inserted, line, removed.count("\n"), text.count("\n"))
        for listener in tuple(self._listeners):
            listener(change)
        after_change.run(self, start, end, start + inserted, buffer=self)

    @provisional("0.1")
    def invalidate(self, start: int, end: int) -> None:
        """Ask for [START, END) to be redisplayed although its text has not changed."""
        if not self._listeners:
            return
        start, end = self.clamp(start), self.clamp(end)
        line = self.store.line_of(start)
        newlines = self.store.line_of(end) - line
        change = Change(start, end, end, line, newlines, newlines, text_changed=False)
        for listener in tuple(self._listeners):
            listener(change)

    def add_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    # -- markers, cursors, spans --------------------------------------------

    @provisional("0.1")
    def marker(self, pos: int, *, advance: bool = False) -> Marker:
        return Marker(self, pos, advance=advance)

    @provisional("0.1")
    def cursor(self, pos: int = 0) -> Cursor:
        return Cursor(self, pos)

    @provisional("0.1")
    def layer(self, name: str) -> SpanLayer:
        """The span layer called NAME, created on first use."""
        layer = self._layers.get(name)
        if layer is None:
            layer = self._layers[name] = SpanLayer(self, name)
        return layer

    @property
    def layers(self) -> list[SpanLayer]:
        return list(self._layers.values())

    # -- searching ----------------------------------------------------------

    @provisional("0.1")
    def search(
        self,
        pattern: str | re.Pattern[str],
        start: int = 0,
        end: int | None = None,
        *,
        backward: bool = False,
    ) -> re.Match[str] | None:
        """Search for a regular expression in [START, END).

        Forward, return the first match.  Backward, return the last match that
        ends at or before END.  Match offsets are buffer positions.
        """
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        text = self.store.snapshot()
        stop = len(text) if end is None else end
        if not backward:
            return compiled.search(text, start, stop)
        last: re.Match[str] | None = None
        for match in compiled.finditer(text, start, stop):
            last = match
        return last

    # -- grapheme clusters --------------------------------------------------

    def _clusters_of_line(self, pos: int) -> tuple[int, int, list[int] | None]:
        """(line start, line end, cluster boundaries or None if ASCII) for the line at POS."""
        cache = self._cluster_cache
        if cache is not None and cache[0] <= pos <= cache[1] and cache[2] == self.tick:
            return cache[0], cache[1], cache[3]
        line = self.store.line_of(pos)
        start, end = self.line_start(line), self.line_end(line)
        bounds = graphemes.boundaries(self.store.slice(start, end))
        self._cluster_cache = (start, end, self.tick, bounds)
        return start, end, bounds

    @provisional("0.1")
    def next_boundary(self, pos: int) -> int:
        """The nearest grapheme cluster boundary after POS."""
        size = len(self.store)
        if pos >= size:
            return size
        start, end, bounds = self._clusters_of_line(pos)
        if pos >= end:
            return pos + 1  # the newline is a cluster of its own
        if bounds is None:
            return pos + 1
        return start + bounds[bisect_right(bounds, pos - start)]

    @provisional("0.1")
    def prev_boundary(self, pos: int) -> int:
        """The nearest grapheme cluster boundary before POS."""
        if pos <= 0:
            return 0
        pos = min(pos, len(self.store))
        start, _, bounds = self._clusters_of_line(pos)
        if pos == start:
            return pos - 1
        if bounds is None:
            return pos - 1
        return start + bounds[bisect_left(bounds, pos - start) - 1]

    @provisional("0.1")
    def is_boundary(self, pos: int) -> bool:
        if pos <= 0 or pos >= len(self.store):
            return True
        start, _, bounds = self._clusters_of_line(pos)
        if bounds is None:
            return True
        offset = pos - start
        i = bisect_left(bounds, offset)
        return i < len(bounds) and bounds[i] == offset

    @provisional("0.1")
    def clusters(self, start: int, end: int) -> Iterator[tuple[int, int]]:
        """The grapheme clusters in [START, END), as (start, end) pairs."""
        pos = start
        while pos < end:
            nxt = min(self.next_boundary(pos), end)
            yield pos, nxt
            pos = nxt

    # -- files --------------------------------------------------------------

    @provisional("0.1")
    def save(self, path: Path | None = None) -> None:
        """Write the buffer to PATH, or to its file."""
        if path is not None:
            self.file = Path(path)
        if self.file is None:
            raise ValueError(f"buffer {self.name} has no file")
        before_save.run(self, buffer=self)
        text = self.store.snapshot()
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        data = text.encode(self.encoding, errors="surrogateescape")
        if self.bom:
            data = codecs.BOM_UTF8 + data
        target = self.file
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            with contextlib.suppress(OSError):
                os.chmod(tmp, target.stat().st_mode & 0o7777)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self.saved_tick = self.tick
        after_save.run(self, buffer=self)

    def __repr__(self) -> str:
        return f"<Buffer {self.name!r} {len(self.store)} chars>"


def decode(data: bytes) -> tuple[str, str, bool, str, bool]:
    """Decode file contents: (text, encoding, bom, newline, used_fallback)."""
    bom = data.startswith(codecs.BOM_UTF8)
    if bom:
        data = data[len(codecs.BOM_UTF8) :]
    fallback = False
    try:
        text = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        encoding = "latin-1"
        fallback = True
    newline = "\n"
    if "\r\n" in text:
        newline = "\r\n"
        text = text.replace("\r\n", "\n")
    if "\r" in text:
        if "\n" not in text:
            newline = "\r"
        text = text.replace("\r", "\n")
    return text, encoding, bom, newline, fallback


@provisional("0.1")
class BufferList:
    """The editor's buffers, by name."""

    def __init__(self, editor: Editor | None = None) -> None:
        self.editor = editor
        self._buffers: dict[str, Buffer] = {}

    def _unique(self, name: str) -> str:
        if name not in self._buffers:
            return name
        n = 2
        while f"{name}<{n}>" in self._buffers:
            n += 1
        return f"{name}<{n}>"

    @provisional("0.1")
    def new(self, name: str, text: str = "") -> Buffer:
        """Create a buffer.  NAME is made unique if it is taken."""
        buffer = Buffer(self._unique(name), text)
        buffer.undo.clear()
        self._buffers[buffer.name] = buffer
        buffer_created.run(buffer, buffer=buffer)
        return buffer

    @provisional("0.1")
    def get(self, name: str) -> Buffer | None:
        return self._buffers.get(name)

    @provisional("0.1")
    def find_file(self, path: Path | str) -> Buffer | None:
        """The buffer already visiting PATH, if any."""
        resolved = Path(path).expanduser().resolve()
        for buffer in self._buffers.values():
            if buffer.file is not None and buffer.file == resolved:
                return buffer
        return None

    @provisional("0.1")
    def visit(self, path: Path | str) -> Buffer:
        """The buffer visiting PATH, reading the file if it is not open yet."""
        resolved = Path(path).expanduser().resolve()
        existing = self.find_file(resolved)
        if existing is not None:
            return existing
        text, encoding, bom, newline, fallback = "", "utf-8", False, "\n", False
        if resolved.exists():
            text, encoding, bom, newline, fallback = decode(resolved.read_bytes())
        buffer = Buffer(self._unique(resolved.name), text, file=resolved)
        buffer.encoding, buffer.bom, buffer.newline = encoding, bom, newline
        buffer.decode_fallback = fallback
        self._buffers[buffer.name] = buffer
        buffer_created.run(buffer, buffer=buffer)
        return buffer

    @provisional("0.1")
    def kill(self, buffer: Buffer) -> None:
        if self._buffers.get(buffer.name) is buffer:
            buffer_killed.run(buffer, buffer=buffer)
            del self._buffers[buffer.name]

    def __iter__(self) -> Iterator[Buffer]:
        return iter(list(self._buffers.values()))

    def __len__(self) -> int:
        return len(self._buffers)

    def __contains__(self, name: object) -> bool:
        return name in self._buffers
