from __future__ import annotations

from collections.abc import Iterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neomacs.text import store as store_module
from neomacs.text.store import ChunkedStore

ALPHABET = st.sampled_from(list("ab \n\n\néß中👍́"))
TEXT = st.text(ALPHABET, max_size=60)


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Small chunks make every test cross chunk boundaries.
    monkeypatch.setattr(store_module, "TARGET", 8)
    monkeypatch.setattr(store_module, "MAX", 16)
    yield


def check(store: ChunkedStore, model: str) -> None:
    assert len(store) == len(model)
    assert store.snapshot() == model
    lines = model.split("\n")
    assert store.line_count() == len(lines)
    pos = 0
    for number, line in enumerate(lines, start=1):
        assert store.line_start(number) == pos
        assert store.line_of(pos) == number
        assert store.line_of(pos + len(line)) == number
        pos += len(line) + 1
    assert all(0 < size <= store_module.MAX for size in store._chunk_sizes())
    for p in range(0, len(model) + 1, max(1, len(model) // 7)):
        b = len(model[:p].encode())
        assert store.byte_of(p) == b
        assert store.pos_of_byte(b) == p
        assert store.slice(p, len(model)) == model[p:]
        assert store.slice(0, p) == model[:p]


@st.composite
def edits(draw: st.DrawFn) -> list[tuple[float, float, str]]:
    return draw(
        st.lists(
            st.tuples(st.floats(0, 1), st.floats(0, 1), TEXT),
            max_size=25,
        )
    )


@settings(max_examples=300, deadline=None)
@given(initial=TEXT, ops=edits())
def test_store_matches_string_model(initial: str, ops: list[tuple[float, float, str]]) -> None:
    store = ChunkedStore(initial)
    model = initial
    check(store, model)
    for a, b, text in ops:
        start = int(a * len(model))
        end = start + int(b * (len(model) - start))
        store.replace(start, end, text)
        model = model[:start] + text + model[end:]
        # Query between edits without a snapshot, to exercise the chunk paths.
        assert store.slice(start, min(len(model), start + 5)) == model[start : start + 5]
        assert store.line_of(start) == model.count("\n", 0, start) + 1
    check(store, model)


def test_empty_store() -> None:
    store = ChunkedStore()
    assert len(store) == 0
    assert store.line_count() == 1
    assert store.line_start(1) == 0
    assert store.line_of(0) == 1
    assert store.slice(0, 0) == ""
    assert store.byte_of(0) == 0
    store.replace(0, 0, "x\ny")
    assert store.snapshot() == "x\ny"
    store.replace(0, 3, "")
    assert store.snapshot() == "" and store.line_count() == 1


def test_bounds() -> None:
    store = ChunkedStore("abc")
    with pytest.raises(IndexError):
        store.replace(2, 5, "")
    with pytest.raises(IndexError):
        store.replace(2, 1, "")
    with pytest.raises(IndexError):
        store.line_start(3)
    with pytest.raises(IndexError):
        store.line_of(9)


def test_large_text_is_chunked() -> None:
    text = "line\n" * 1000
    store = ChunkedStore(text)
    assert len(store._chunk_sizes()) > 100
    assert store.line_start(1000) == 4995
    assert store.line_of(4995) == 1000
    store.replace(0, len(text), "")
    assert store._chunk_sizes() == []
