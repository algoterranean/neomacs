from __future__ import annotations

import gc
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neomacs import _owner
from neomacs.errors import ReadOnly
from neomacs.hooks import Hook
from neomacs.text import Buffer, BufferList, Change
from neomacs.text import buffer as buffer_module
from neomacs.text.buffer import decode

TEXT = st.text(st.sampled_from(list("ab \n\néß👍")), max_size=40)
EDITS = st.lists(st.tuples(st.floats(0, 1), st.floats(0, 1), TEXT), max_size=20)


def test_reading() -> None:
    b = Buffer("t", "one\ntwo\nthree")
    assert len(b) == 13
    assert b[0] == "o" and b[-1] == "e" and b[4:7] == "two" and b[:3] == "one"
    assert b[::2] == "oetotre"
    assert b.line_count == 3
    assert [b.line_text(n) for n in (1, 2, 3)] == ["one", "two", "three"]
    assert b.line_start(2) == 4 and b.line_end(2) == 7 and b.line_end(3) == 13
    assert b.position(2, 1) == 5 and b.position(2, 99) == 7
    assert b.line_offset(5) == (2, 1)
    with pytest.raises(IndexError):
        b[13]
    with pytest.raises(IndexError):
        b.line_end(4)


def test_insert_delete_replace() -> None:
    b = Buffer("t", "hello")
    b.insert(5, " world")
    assert b.delete(0, 6) == "hello "
    b.replace(0, 5, "there")
    assert b.text == "there"
    assert b.tick == 3 and b.modified
    b.replace(2, 2, "")  # nothing to do: no tick
    assert b.tick == 3
    with pytest.raises(IndexError):
        b.replace(3, 99, "")


def test_carriage_returns_never_enter_a_buffer() -> None:
    b = Buffer("t")
    b.insert(0, "a\r\nb\rc")
    assert b.text == "a\nb\nc"


def test_read_only() -> None:
    b = Buffer("t", "x")
    b.read_only = True
    with pytest.raises(ReadOnly):
        b.insert(0, "y")
    assert b.text == "x"


def test_change_pipeline_order() -> None:
    b = Buffer("t", "ab\ncd")
    events: list[object] = []
    m = b.marker(5)

    def before(buf: Buffer, start: int, end: int) -> None:
        events.append(("before", buf.text, start, end, m.pos, buf.tick))

    def after(buf: Buffer, start: int, old_end: int, new_end: int) -> None:
        events.append(("after", buf.text, start, old_end, new_end, m.pos, buf.tick))

    buffer_module.before_change.add(before, buffer=b)
    buffer_module.after_change.add(after, buffer=b)
    b.add_listener(events.append)
    b.replace(1, 3, "X\nY\nZ")
    assert events == [
        ("before", "ab\ncd", 1, 3, 5, 0),
        Change(1, 3, 6, line=1, old_newlines=1, new_newlines=2, removed="b\n"),
        ("after", "aX\nY\nZcd", 1, 3, 6, 8, 1),
    ]
    # Hooks for one buffer do not fire for another.
    Buffer("other", "q").insert(0, "z")
    assert len(events) == 3


def test_marker_rules() -> None:
    b = Buffer("t", "0123456789")
    stay, adv = b.marker(5), b.marker(5, advance=True)
    b.insert(5, "xx")  # insertion exactly at the marker
    assert (stay.pos, adv.pos) == (5, 7)
    b.insert(0, "y")  # before
    assert (stay.pos, adv.pos) == (6, 8)
    b.insert(len(b), "z")  # after
    assert (stay.pos, adv.pos) == (6, 8)
    b = Buffer("t", "0123456789")
    before, start, inside, end, after = (b.marker(p) for p in (1, 3, 5, 7, 9))
    inside_adv = b.marker(5, advance=True)
    b.replace(3, 7, "ab")
    assert b.text == "012ab789"
    assert [m.pos for m in (before, start, inside, inside_adv, end, after)] == [1, 3, 3, 5, 5, 7]


def test_markers_are_weak_and_detachable() -> None:
    b = Buffer("t", "abc")
    b.marker(1)
    gc.collect()
    assert len(b._markers) == 0
    m = b.marker(1)
    m.detach()
    b.insert(0, "xx")
    assert m.pos == 1
    m.pos = 99
    assert m.pos == len(b)


@settings(max_examples=200, deadline=None)
@given(initial=TEXT, ops=EDITS, where=st.floats(0, 1))
def test_markers_follow_untouched_text(
    initial: str, ops: list[tuple[float, float, str]], where: float
) -> None:
    b = Buffer("t", initial)
    m = b.marker(int(where * len(initial)))
    for a, c, text in ops:
        start = int(a * len(b))
        end = start + int(c * (len(b) - start))
        p = m.pos
        b.replace(start, end, text)
        if p < start:
            assert m.pos == p
        elif p > end:
            assert m.pos == p + len(text) - (end - start)
        else:
            assert start <= m.pos <= start + len(text)
        assert 0 <= m.pos <= len(b)


@settings(max_examples=200, deadline=None)
@given(initial=TEXT, groups=st.lists(EDITS, max_size=6))
def test_undo_and_redo_restore_text(
    initial: str, groups: list[list[tuple[float, float, str]]]
) -> None:
    b = Buffer("t", initial)
    states = [initial]
    for ops in groups:
        with b.undo.group():
            for a, c, text in ops:
                start = int(a * len(b))
                end = start + int(c * (len(b) - start))
                b.replace(start, end, text)
        if b.text != states[-1] or any(True for _ in ops):
            states.append(b.text)
    final = b.text
    while b.undo.undo() is not None:
        pass
    assert b.text == initial
    while b.undo.redo() is not None:
        pass
    assert b.text == final


def test_undo_groups_and_redo_discard() -> None:
    b = Buffer("t")
    with b.undo.group():
        b.insert(0, "a")
        b.insert(1, "b")
    b.insert(2, "c")  # its own group
    assert b.undo.undo() is not None and b.text == "ab"
    assert b.undo.undo() is not None and b.text == ""
    assert b.undo.undo() is None
    assert b.undo.redo() is not None and b.text == "ab"
    b.insert(2, "!")  # a new edit discards the redo stack
    assert not b.undo.can_redo
    assert b.text == "ab!"


def test_undo_moves_markers_like_any_edit() -> None:
    b = Buffer("t", "abc")
    m = b.marker(3)
    b.insert(0, "xx")
    assert m.pos == 5
    b.undo.undo()
    assert m.pos == 3


def test_search() -> None:
    b = Buffer("t", "foo bar foo baz")
    m = b.search("foo")
    assert m is not None and m.span() == (0, 3)
    m = b.search("foo", 1)
    assert m is not None and m.span() == (8, 11)
    m = b.search(re.compile(r"ba."), backward=True)
    assert m is not None and m.group() == "baz"
    m = b.search("ba.", end=10, backward=True)
    assert m is not None and m.group() == "bar"
    assert b.search("nope") is None


def test_spans_track_edits() -> None:
    b = Buffer("t", "hello world")
    layer = b.layer("test")
    assert b.layer("test") is layer
    span = layer.add(6, 11, face="bold")
    b.insert(0, ">> ")
    assert (span.start, span.end) == (9, 14)
    b.insert(9, "X")  # typed at the start: stays outside
    assert (span.start, span.end) == (10, 15)
    b.insert(15, "Y")  # typed at the end: stays outside
    assert (span.start, span.end) == (10, 15)
    b.insert(12, "--")  # typed inside: grows
    assert (span.start, span.end) == (10, 17)
    assert b[span.start : span.end] == "wo--rld"
    b.delete(8, len(b))
    assert span.start == span.end == 8


def test_span_queries_and_invalidation() -> None:
    b = Buffer("t", "one\ntwo\nthree\n")
    seen: list[Change] = []
    b.add_listener(seen.append)
    layer = b.layer("x")
    low = layer.add(0, 7, priority=0, face="a")
    high = layer.add(4, 13, priority=5, face="b")
    assert layer.at(5) == [high, low]
    assert layer.at(1) == [low]
    assert layer.overlapping(0, 4) == [low]
    assert layer.overlapping(7, 8) == [high]
    assert [s.get("face") for s in layer] == ["a", "b"]
    assert all(not c.text_changed for c in seen)
    assert (seen[1].line, seen[1].old_newlines, seen[1].new_newlines) == (2, 1, 1)
    count = len(seen)
    high.set(face="c")
    high.remove()
    assert len(seen) == count + 2 and len(layer) == 1
    layer.clear()
    assert len(layer) == 0


def test_hooks_are_idempotent_and_isolated() -> None:
    hook = Hook("test_hook")
    calls: list[str] = []

    def f(x: str) -> None:
        calls.append("f" + x)

    hook.add(f)
    hook.add(f)

    def f(x: str) -> None:  # noqa: F811 - redefinition replaces, as re-evaluation would
        calls.append("F" + x)

    hook.add(f)
    hook.run("1")
    assert calls == ["F1"]
    hook.remove(f)
    hook.run("2")
    assert calls == ["F1"]


def test_owner_thread_is_enforced() -> None:
    import threading

    b = Buffer("t", "x")
    _owner.claim()
    try:
        errors: list[BaseException] = []

        def other() -> None:
            try:
                b.insert(0, "y")
            except RuntimeError as exc:
                errors.append(exc)

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert errors and "does not own" in str(errors[0])
        b.insert(0, "z")  # the owner may
    finally:
        _owner.release()
    assert b.text == "zx"


# -- files ------------------------------------------------------------------


def test_decode() -> None:
    assert decode(b"a\nb") == ("a\nb", "utf-8", False, "\n", False)
    assert decode(b"\xef\xbb\xbfa") == ("a", "utf-8", True, "\n", False)
    assert decode(b"a\r\nb\r\n") == ("a\nb\n", "utf-8", False, "\r\n", False)
    assert decode(b"a\rb") == ("a\nb", "utf-8", False, "\r", False)
    assert decode(b"caf\xe9") == ("café", "latin-1", False, "\n", True)


@pytest.mark.parametrize(
    "data",
    [b"plain\ntext\n", b"\xef\xbb\xbfbom\r\ncrlf\r\n", b"caf\xe9 latin\n", "ünï\n".encode()],
)
def test_file_round_trip(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(data)
    buffers = BufferList()
    b = buffers.visit(path)
    assert "\r" not in b.text
    assert buffers.visit(path) is b
    b.insert(0, "x")
    b.delete(0, 1)
    assert b.modified
    b.save()
    assert not b.modified
    assert path.read_bytes() == data


def test_save_replaces_atomically_and_runs_hooks(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "new.txt"
    buffers = BufferList()
    b = buffers.visit(path)  # does not exist yet
    assert b.text == "" and b.file == path.resolve()
    seen: list[str] = []
    buffer_module.before_save.add(lambda buf: seen.append("before"), buffer=b)
    buffer_module.after_save.add(lambda buf: seen.append("after"), buffer=b)
    b.insert(0, "content\n")
    b.save()
    assert path.read_text() == "content\n"
    assert seen == ["before", "after"]
    assert [p.name for p in path.parent.iterdir()] == ["new.txt"]
    other = tmp_path / "copy.txt"
    b.save(other)
    assert other.read_text() == "content\n" and b.file == other


def test_buffer_list(tmp_path: Path) -> None:
    buffers = BufferList()
    a = buffers.new("scratch", "x")
    b = buffers.new("scratch")
    assert (a.name, b.name) == ("scratch", "scratch<2>")
    assert not a.undo.can_undo
    assert buffers.get("scratch") is a and "scratch<2>" in buffers and len(buffers) == 2
    buffers.kill(a)
    assert buffers.get("scratch") is None and list(buffers) == [b]
    with pytest.raises(ValueError):
        b.save()
