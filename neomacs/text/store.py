"""Text storage (ENGINE 5.1).

Positions are 0-based code point offsets; lines are 1-based.  The store is
behind a small protocol so a native implementation can replace it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from itertools import accumulate
from typing import Protocol

TARGET = 2048  # preferred chunk size, in code points
MAX = 4096  # a chunk larger than this is split


class TextStore(Protocol):
    def __len__(self) -> int: ...
    def slice(self, start: int, end: int) -> str: ...
    def replace(self, start: int, end: int, text: str) -> None: ...
    def line_count(self) -> int: ...
    def line_start(self, line: int) -> int: ...
    def line_of(self, pos: int) -> int: ...
    def snapshot(self) -> str: ...
    def byte_of(self, pos: int) -> int: ...
    def pos_of_byte(self, byte: int) -> int: ...


def _split(text: str) -> list[str]:
    if len(text) <= MAX:
        return [text] if text else []
    return [text[i : i + TARGET] for i in range(0, len(text), TARGET)]


def _bytes(chunk: str) -> int:
    return len(chunk) if chunk.isascii() else len(chunk.encode("utf-8", "surrogatepass"))


class ChunkedStore:
    """Text as a list of string chunks with per-chunk counts.

    An edit rewrites the chunk or chunks it touches.  Prefix sums over the
    counts are rebuilt lazily, so a run of edits pays for them once.
    """

    def __init__(self, text: str = "") -> None:
        self._chunks: list[str] = _split(text)
        self._lens: list[int] = [len(c) for c in self._chunks]
        self._nls: list[int] = [c.count("\n") for c in self._chunks]
        self._bys: list[int] = [_bytes(c) for c in self._chunks]
        self._starts: list[int] | None = None  # position of each chunk's first character
        self._nl_before: list[int] | None = None
        self._by_before: list[int] | None = None
        self._total = len(text)
        self._newlines = sum(self._nls)
        self._snapshot: str | None = text

    # -- indexes --------------------------------------------------------------

    def _index(self) -> tuple[list[int], list[int], list[int]]:
        if self._starts is None or self._nl_before is None or self._by_before is None:
            self._starts = list(accumulate(self._lens, initial=0))[:-1]
            self._nl_before = list(accumulate(self._nls, initial=0))[:-1]
            self._by_before = list(accumulate(self._bys, initial=0))[:-1]
        return self._starts, self._nl_before, self._by_before

    def _locate(self, pos: int) -> tuple[int, int]:
        """Chunk index and offset within it for POS.  The store must not be empty."""
        starts = self._index()[0]
        i = bisect_right(starts, pos) - 1
        return i, pos - starts[i]

    def _check(self, pos: int) -> None:
        if not 0 <= pos <= self._total:
            raise IndexError(f"position {pos} out of range 0..{self._total}")

    # -- protocol ---------------------------------------------------------------

    def __len__(self) -> int:
        return self._total

    def slice(self, start: int, end: int) -> str:
        self._check(start)
        self._check(end)
        if start >= end:
            return ""
        if self._snapshot is not None:
            return self._snapshot[start:end]
        i, off_i = self._locate(start)
        j, off_j = self._locate(end)
        if i == j:
            return self._chunks[i][off_i:off_j]
        parts = [self._chunks[i][off_i:]]
        parts.extend(self._chunks[i + 1 : j])
        parts.append(self._chunks[j][:off_j])
        return "".join(parts)

    def replace(self, start: int, end: int, text: str) -> None:
        self._check(start)
        self._check(end)
        if start > end:
            raise IndexError(f"start {start} after end {end}")
        if start == end and not text:
            return
        if not self._chunks:
            pieces = _split(text)
            first, last = 0, -1
        else:
            first, off_i = self._locate(start)
            last, off_j = self._locate(end)
            merged = self._chunks[first][:off_i] + text + self._chunks[last][off_j:]
            # Fold a small remainder into its left neighbour so deletions do
            # not leave a trail of tiny chunks.
            if (
                len(merged) < TARGET // 4
                and first > 0
                and self._lens[first - 1] + len(merged) <= MAX
            ):
                first -= 1
                merged = self._chunks[first] + merged
            pieces = _split(merged)
        self._chunks[first : last + 1] = pieces
        self._lens[first : last + 1] = [len(p) for p in pieces]
        self._nls[first : last + 1] = [p.count("\n") for p in pieces]
        self._bys[first : last + 1] = [_bytes(p) for p in pieces]
        self._total += len(text) - (end - start)
        self._starts = self._nl_before = self._by_before = None
        self._newlines = -1
        self._snapshot = None

    def line_count(self) -> int:
        if self._newlines < 0:
            self._newlines = sum(self._nls)
        return self._newlines + 1

    def line_start(self, line: int) -> int:
        """Position of the first character of 1-based LINE."""
        if not 1 <= line <= self.line_count():
            raise IndexError(f"line {line} out of range 1..{self.line_count()}")
        need = line - 1  # newlines that must precede the position
        if need == 0:
            return 0
        starts, nl_before, _ = self._index()
        # The last chunk with fewer than NEED newlines before it holds the one we want.
        i = bisect_left(nl_before, need) - 1
        chunk = self._chunks[i]
        at = -1
        for _ in range(need - nl_before[i]):
            at = chunk.index("\n", at + 1)
        return starts[i] + at + 1

    def line_of(self, pos: int) -> int:
        """The 1-based line containing POS."""
        self._check(pos)
        if not self._chunks:
            return 1
        i, off = self._locate(pos)
        return self._index()[1][i] + self._chunks[i].count("\n", 0, off) + 1

    def snapshot(self) -> str:
        if self._snapshot is None:
            self._snapshot = "".join(self._chunks)
        return self._snapshot

    def byte_of(self, pos: int) -> int:
        """UTF-8 byte offset of POS."""
        self._check(pos)
        if not self._chunks:
            return 0
        i, off = self._locate(pos)
        return self._index()[2][i] + _bytes(self._chunks[i][:off])

    def pos_of_byte(self, byte: int) -> int:
        """Position of UTF-8 byte offset BYTE, which must fall on a character boundary."""
        if byte == 0 or not self._chunks:
            return 0
        starts, _, by_before = self._index()
        i = bisect_right(by_before, byte) - 1
        chunk = self._chunks[i]
        within = byte - by_before[i]
        if chunk.isascii():
            return starts[i] + within
        prefix = chunk.encode("utf-8", "surrogatepass")[:within]
        return starts[i] + len(prefix.decode("utf-8", "surrogatepass"))

    # -- for tests ----------------------------------------------------------------

    def _chunk_sizes(self) -> list[int]:
        return list(self._lens)
