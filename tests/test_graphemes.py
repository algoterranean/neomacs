from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from neomacs.text import Buffer

DATA = Path(__file__).parent / "data" / f"GraphemeBreakTest-{unicodedata.unidata_version}.txt"

CORPUS = [
    ("é", "combining accent"),
    ("👍🏽", "skin tone modifier"),
    ("🇯🇵", "flag"),
    ("👨‍👩‍👧", "joined family"),
    ("क्षि", "Devanagari syllable"),
    ("각", "Hangul syllable"),
    ("각", "Hangul jamo sequence"),
]


def conformance_cases() -> list[tuple[list[str], str]]:
    cases: list[tuple[list[str], str]] = []
    for raw in DATA.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        clusters = [
            "".join(chr(int(cp, 16)) for cp in part.split("×") if cp.strip())
            for part in line.strip("÷").split("÷")
        ]
        cases.append(([c for c in clusters if c], raw))
    return cases


@pytest.mark.skipif(not DATA.exists(), reason="no break test data for this Unicode version")
def test_unicode_conformance() -> None:
    cases = conformance_cases()
    assert len(cases) > 500
    checked = 0
    for clusters, raw in cases:
        text = "".join(clusters)
        if "\n" in text or "\r" in text:
            continue  # a buffer line never contains a line break
        b = Buffer("t", text)
        expected = [0]
        for cluster in clusters:
            expected.append(expected[-1] + len(cluster))
        forward = [0]
        while forward[-1] < len(b):
            forward.append(b.next_boundary(forward[-1]))
        assert forward == expected, raw
        backward = [len(b)]
        while backward[-1] > 0:
            backward.append(b.prev_boundary(backward[-1]))
        assert backward[::-1] == expected, raw
        for pos in range(len(text) + 1):
            assert b.is_boundary(pos) == (pos in expected), raw
        checked += 1
    assert checked > 400


@pytest.mark.parametrize(("cluster", "what"), CORPUS)
def test_motion_and_deletion_take_one_step(cluster: str, what: str) -> None:
    b = Buffer("t", f"a{cluster}b\nnext")
    c = b.cursor(1)
    c.forward_char()
    assert c.pos == 1 + len(cluster), what
    c.backward_char()
    assert c.pos == 1, what
    assert list(b.clusters(0, c.buffer.line_end(1))) == [
        (0, 1),
        (1, 1 + len(cluster)),
        (1 + len(cluster), 2 + len(cluster)),
    ]
    c.move_to(1 + len(cluster))
    assert c.column == 2 and c.offset == 1 + len(cluster)
    assert c.delete_backward_char() == cluster
    assert b.text == "ab\nnext"
    b.insert(1, cluster)
    c.move_to(1)
    assert c.delete_char() == cluster


def test_ascii_fast_path_and_line_edges() -> None:
    b = Buffer("t", "ab\ncd")
    assert [b.next_boundary(p) for p in range(6)] == [1, 2, 3, 4, 5, 5]
    assert [b.prev_boundary(p) for p in range(6)] == [0, 0, 1, 2, 3, 4]
    assert all(b.is_boundary(p) for p in range(6))


def test_cluster_cache_is_dropped_on_edit() -> None:
    b = Buffer("t", "éx")
    assert b.next_boundary(0) == 2
    b.delete(1, 2)
    assert b.next_boundary(0) == 1
    b.insert(1, "́")
    assert b.next_boundary(0) == 2 and not b.is_boundary(1)


def test_cursor_motion() -> None:
    b = Buffer("t", "héllo\nwörld 👨‍👩‍👧 x\nz")
    c = b.cursor()
    c.forward_line()
    c.end_of_line()
    assert (c.line, c.offset, c.column) == (2, 13, 9)
    c.forward_line()  # the last line is shorter: clamp
    assert (c.line, c.offset) == (3, 1)
    c.forward_line(-2)
    assert (c.line, c.offset) == (1, 1)
    c.start_of_line()
    c.forward_char(-1)
    assert c.pos == 0
    c.forward_line(1)
    c.forward_char(8)
    c.forward_line(-1)  # offset 8 is past the end of line 1
    assert c.pos == 5
    c.insert("!")
    assert c.pos == 6 and b.line_text(1) == "héllo!"
    b2 = Buffer("t", "ab\n👨‍👩‍👧")
    c2 = b2.cursor(2)
    c2.forward_line()  # offset 2 falls inside the family emoji: snap back
    assert c2.pos == 3
