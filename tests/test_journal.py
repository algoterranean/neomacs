"""Step 11: the recovery journal for unsaved buffers (ENGINE 11.1)."""

from __future__ import annotations

import json
import os
import signal
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from neomacs.testing import Client, running_engine
from neomacs.testing.headless import HeadlessUI


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.01)


@contextmanager
def engine(data: Path, *, quick: bool = True) -> Iterator[HeadlessUI]:
    """An engine keeping its journal under DATA, with a frontend attached."""
    with running_engine(env={"NEOMACS_DATA_DIR": str(data)}) as path:
        with HeadlessUI(path, rows=12, cols=100) as ui:
            if quick:
                ui.eval("ed.options.journal_delay = 0.02")
            yield ui


def entries(data: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in sorted((data / "journal").glob("*/*.json")):
        try:
            found.append(json.loads(path.read_text()))
        except ValueError:
            pass  # being written
    return found


def texts(data: Path) -> dict[str, str]:
    return {e["name"]: e["text"] for e in entries(data)}


def crash(ui: HeadlessUI) -> None:
    """Stop the engine the way a crash does: at once, with no chance to tidy up."""
    os.kill(value(ui.client, "__import__('os').getpid()"), signal.SIGKILL)
    time.sleep(0.2)


def test_unsaved_text_is_offered_back_after_the_engine_is_killed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    notes = tmp_path / "notes.txt"
    notes.write_text("on disk\n")
    with engine(data) as ui:
        ui.open(notes)
        ui.type("typed and never saved, ")
        ui.eval("_s = ed.buffers.get('*scratch*'); _s.insert(len(_s), 'scratch work')")
        wait_for(
            lambda: (
                texts(data).get("notes.txt") == "typed and never saved, on disk\n"
                and "scratch work" in texts(data).get("*scratch*", "")
            )
        )
        pid = value(ui.client, "__import__('os').getpid()")
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    assert notes.read_text() == "on disk\n"

    with engine(data) as ui:
        # The offer is made to the frame that attaches, unasked.
        assert ui.message == (
            "Unsaved text from 2 buffers was kept when the editor last stopped: M-x recover"
        )
        ui.keys("M-x")
        ui.type("recover")
        ui.keys("<return>")
        listing = value(ui.client, "ed.buffers.get('*recover*').text")
        assert f"notes.txt  {notes.resolve()}  " in listing and "*scratch*  (no file)  " in listing
        assert "31 characters" in listing
        # The cursor is on the first entry; find the file's line and take it.
        ui.eval(
            "_r = ed.buffers.get('*recover*'); _p = _r.text.index('notes.txt'); "
            "ed.view.selections.set([(_p, _p)])"
        )
        ui.keys("<return>")
        assert ui.view.info["name"] == "notes.txt"
        assert ui.text_rows_shown() == [" 1 typed and never saved, on disk", " 2"]
        assert ui.message == "Recovered notes.txt. It is not saved yet."
        assert value(ui.client, "(ed.buffer.modified, ed.buffer.file.name)") == (True, "notes.txt")
        assert notes.read_text() == "on disk\n"  # nothing is written until the user saves
        # Undoing gives back what the file has.
        ui.keys("C-/")
        assert value(ui.client, "ed.buffer.text") == "on disk\n"
        ui.keys("M-/")
        # The entry taken has gone from the list; the other is still there.
        listing = value(ui.client, "ed.buffers.get('*recover*').text")
        assert "notes.txt" not in listing and "*scratch*" in listing
        ui.keys("C-x", "C-s")
        assert notes.read_text() == "typed and never saved, on disk\n"
        # Now the scratch buffer's.
        ui.eval(
            "_w = ed.frame.window; _w.show(_r); ed.frame.focus_view(_w); "
            "_p = _r.text.index('*scratch*'); _w.selections.set([(_p, _p)])"
        )
        ui.keys("<return>")
        assert value(ui.client, "ed.buffer.name") == "*scratch*"
        assert value(ui.client, "ed.buffer.text").endswith("scratch work")
        assert value(ui.client, "ed.buffers.get('*recover*').text") == "Nothing to recover.\n"
        # The recovered text is this engine's unsaved text now.  Its journal
        # is written a moment after the edit: wait for that, or the engine
        # may be gone first and leave nothing for the next to offer.
        wait_for(lambda: "scratch work" in texts(data).get("*scratch*", ""))
        ui.client.notify("input.key", "C-x")
        ui.client.notify("input.key", "C-c")

    # Nothing is left to offer a third engine but the scratch text, which was
    # recovered and never saved (it has no file), so it is still unsaved.
    with engine(data) as ui:
        assert "Unsaved text from a buffer" in ui.message
        left = value(ui.client, "[(e.name, e.file) for e in ed.journal.left_behind()]")
        assert left == [("*scratch*", None)]


def test_an_entry_goes_when_its_buffer_is_saved_or_killed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x\n")
    with engine(data) as ui:
        for name in ("a.txt", "b.txt", "c.txt"):
            ui.open(tmp_path / name)
            ui.type("new ")
        wait_for(lambda: sorted(texts(data)) == ["a.txt", "b.txt", "c.txt"])
        # c: saved.  a: killed.  b stays: it is still unsaved.
        ui.keys("C-x", "C-s")
        wait_for(lambda: sorted(texts(data)) == ["a.txt", "b.txt"])
        ui.open(tmp_path / "a.txt")
        ui.call("buffers.kill")
        wait_for(lambda: sorted(texts(data)) == ["b.txt"])
        # Edited again after being saved: it comes back.
        ui.open(tmp_path / "c.txt")
        ui.type("more ")
        wait_for(lambda: texts(data) == {"b.txt": "new x\n", "c.txt": "new more x\n"})


def test_buffers_the_editor_writes_itself_are_not_kept(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval(
            "ed.message('into the messages buffer')\n"
            "_h = ed.buffers.new('*made by a command*'); _h.insert(0, 'generated')\n"
            "_u = ed.buffers.new('untitled'); _u.insert(0, 'mine')\n"
            "_n = ed.buffers.new('private'); _n.locals['no_journal'] = True; _n.insert(0, 'secret')"
        )
        ui.keys("M-x")
        ui.type("goto")  # a prompt's own text
        wait_for(lambda: "untitled" in texts(data))
        time.sleep(0.2)
        assert texts(data) == {"untitled": "mine"}
        ui.keys("C-g")


def test_a_clean_exit_keeps_what_is_unsaved_and_writes_what_was_waiting(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (tmp_path / "a.txt").write_text("x\n")
    with engine(data, quick=False) as ui:
        ui.eval("ed.options.journal_delay = 60.0")  # so nothing is written before the exit
        ui.open(tmp_path / "a.txt")
        ui.type("last words ")
        assert texts(data) == {}
        ui.client.notify("engine.call", "engine.exit", [])
        wait_for(lambda: texts(data) == {"a.txt": "last words x\n"})
    with engine(data) as ui:
        assert "Unsaved text from a buffer" in ui.message


def test_an_engine_with_nothing_unsaved_leaves_nothing_behind(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (tmp_path / "a.txt").write_text("x\n")
    with engine(data) as ui:
        ui.open(tmp_path / "a.txt")
        ui.type("y")
        wait_for(lambda: "a.txt" in texts(data))
        ui.keys("C-x", "C-s")
        wait_for(lambda: texts(data) == {})
        ui.client.notify("input.key", "C-x")
        ui.client.notify("input.key", "C-c")
        wait_for(lambda: list((data / "journal").iterdir()) == [])
    with engine(data) as ui:
        assert ui.message == ""
        ui.keys("M-x")
        ui.type("recover")
        ui.keys("<return>")
        assert value(ui.client, "ed.buffers.get('*recover*').text") == "Nothing to recover.\n"


def test_a_running_engines_text_is_not_offered_to_another(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as first:
        first.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'in use')")
        wait_for(lambda: texts(data) == {"untitled": "in use"})
        with engine(data) as second:
            assert second.message == ""
            assert value(second.client, "ed.journal.left_behind()") == []
            second.eval("_u = ed.buffers.new('other'); _u.insert(0, 'theirs')")
            wait_for(lambda: sorted(texts(data)) == ["other", "untitled"])
            assert value(first.client, "ed.journal.left_behind()") == []
            crash(second)
        # The second has died: the first can now have its text, and only its.
        left = value(first.client, "[(e.name, e.text) for e in ed.journal.left_behind()]")
        assert left == [("other", "theirs")]


def test_discarding_an_entry(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'not wanted')")
        wait_for(lambda: "untitled" in texts(data))
        crash(ui)
    with engine(data) as ui:
        ui.keys("M-x")
        ui.type("recover")
        ui.keys("<return>")
        ui.keys("d")
        assert ui.message == "Discarded the unsaved text of untitled"
        assert value(ui.client, "ed.buffers.get('*recover*').text") == "Nothing to recover.\n"
        assert value(ui.client, "ed.buffers.get('untitled')") is None
        assert texts(data) == {}
        ui.keys("d")
        assert ui.message == "No unsaved text on this line"


def test_a_file_that_changed_meanwhile_is_pointed_out(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for name in ("same.txt", "changed.txt", "deleted.txt"):
        (tmp_path / name).write_text("x\n")
    with engine(data) as ui:
        for name in ("same.txt", "changed.txt", "deleted.txt"):
            ui.open(tmp_path / name)
            ui.type("edit ")
        # All of what was typed, not the entry as it was part of the way through.
        wait_for(lambda: list(texts(data).values()) == ["edit x\n"] * 3)
        crash(ui)
    (tmp_path / "changed.txt").write_text("somebody else wrote this\n")
    (tmp_path / "deleted.txt").unlink()
    with engine(data) as ui:
        flags = value(ui.client, "{e.name: e.file_changed for e in ed.journal.left_behind()}")
        assert flags == {"same.txt": False, "changed.txt": True, "deleted.txt": True}
        ui.keys("M-x")
        ui.type("recover")
        ui.keys("<return>")
        lines = value(ui.client, "ed.buffers.get('*recover*').text").split("\n")
        assert [("has changed since" in line) for line in lines if ".txt" in line] == [
            "same" not in line for line in lines if ".txt" in line
        ]
        # A deleted file's text can still be had: the buffer is for that path.
        ui.eval(
            "_r = ed.buffers.get('*recover*'); _p = _r.text.index('deleted.txt'); "
            "ed.view.selections.set([(_p, _p)])"
        )
        ui.keys("<return>")
        assert value(ui.client, "(ed.buffer.text, ed.buffer.modified)") == ("edit x\n", True)


def test_text_that_is_not_valid_unicode_survives(tmp_path: Path) -> None:
    data = tmp_path / "data"
    odd = "caf\xe9 \U0001f600 lone \udcff surrogate\tand\x00nul\r\n"
    with engine(data) as ui:
        ui.eval(f"_u = ed.buffers.new('odd'); _u.insert(0, {odd!r})")
        held = value(ui.client, "_u.text")
        assert "\udcff" in held and "\x00" in held and "\U0001f600" in held
        wait_for(lambda: "odd" in texts(data))
        crash(ui)
    with engine(data) as ui:
        ui.eval("_b = ed.journal.recover(ed.journal.left_behind()[0])")
        assert value(ui.client, "_b.text") == held


def test_the_journal_can_be_turned_off_and_long_buffers_are_left_out(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval(
            "ed.options.journal_limit = 100\n"
            "_big = ed.buffers.new('big'); _big.insert(0, 'x' * 500)\n"
            "_small = ed.buffers.new('small'); _small.insert(0, 'y')"
        )
        wait_for(lambda: "small" in texts(data))
        time.sleep(0.2)
        assert sorted(texts(data)) == ["small"]
        assert (
            value(ui.client, "sum('too long for the recovery journal' in m for m in ed.messages)")
            == 1
        )
        ui.eval(
            "ed.options.journal = False; _small.insert(0, 'z'); _o = ed.buffers.new('off'); _o.insert(0, 'q')"
        )
        wait_for(lambda: texts(data) == {})  # switched off: what was kept goes, nothing new comes


def test_journal_files_are_private_and_whole(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'private text')")
        wait_for(lambda: "untitled" in texts(data))
        (session,) = [d for d in (data / "journal").iterdir()]
        assert stat.S_IMODE(session.stat().st_mode) == 0o700
        files = sorted(p.name for p in session.iterdir())
        assert files == ["1.json", "lock"]  # and no half-written temporary file
        assert stat.S_IMODE((session / "1.json").stat().st_mode) == 0o600
        # Many quick edits: every state on disk is a complete one.
        for i in range(30):
            ui.eval(f"_u.insert(len(_u), ' {i}')")
            for entry in entries(data):
                assert entry["text"].startswith("private text")
        wait_for(lambda: texts(data).get("untitled", "").endswith(" 29"))


def test_a_buffer_that_is_costly_to_take_is_written_less_often(tmp_path: Path) -> None:
    # The text is taken on the main loop each time it is written, so the longer
    # that took last time, the longer the wait before the next.
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'a')")
        wait_for(lambda: "untitled" in texts(data))
        assert 0 <= value(ui.client, "ed.journal._cost") < 0.02
        ui.eval("ed.journal._cost = 0.01; _u.insert(0, 'b')")
        assert value(ui.client, "ed.journal._timer.seconds") == 0.5
        ui.eval("ed.journal._timer.cancel(); ed.journal._timer = None; ed.journal._cost = 0.0")
        ui.eval("_u.insert(0, 'c')")
        assert value(ui.client, "ed.journal._timer.seconds") == 0.02


def test_an_older_write_never_replaces_a_newer_one_or_brings_an_entry_back(tmp_path: Path) -> None:
    # A write runs in a worker thread.  The engine may stop, or the buffer be
    # saved, while one is still on its way: whichever was decided later wins,
    # in whatever order they reach the disk.
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval("ed.options.journal_delay = 600.0")  # nothing is written but what is asked for
        ui.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'first')")
        ui.eval("_older = ed.journal._take(); ed.journal._open()")
        ui.eval("_u.insert(len(_u), ' second'); _newer = ed.journal._take()")
        ui.eval("ed.journal._apply(_newer); ed.journal._apply(_older)")
        assert texts(data) == {"untitled": "first second"}
        # Removed, as when the buffer is saved, with a write still on its way.
        ui.eval("_u.insert(len(_u), ' third'); _late = ed.journal._take()")
        ui.eval("ed.journal.forget(_u); ed.journal._apply(_late)")
        assert texts(data) == {}
        # And an entry written after that is not held back by it.
        ui.eval("_u.insert(len(_u), ' fourth'); ed.journal._apply(ed.journal._take())")
        assert texts(data) == {"untitled": "first second third fourth"}


def test_stopping_waits_for_a_write_that_is_under_way(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with engine(data) as ui:
        ui.eval("_u = ed.buffers.new('untitled'); _u.insert(0, 'first')")
        wait_for(lambda: texts(data) == {"untitled": "first"})
        # Make each write slow, change the text twice with a write between, and stop.
        ui.eval(
            "import time, neomacs.journal as _j; _w0 = _j._write\n"
            "def _slow(path, record):\n    time.sleep(0.3)\n    _w0(path, record)\n"
            "_j._write = _slow"
        )
        ui.eval("_u.insert(len(_u), ' second')")
        wait_for(lambda: value(ui.client, "ed.journal._flushing") is True)
        ui.eval("_u.insert(len(_u), ' third')")
        ui.client.notify("engine.call", "engine.exit", [])
        wait_for(lambda: texts(data) == {"untitled": "first second third"})
        time.sleep(0.5)
        assert texts(data) == {"untitled": "first second third"}
        (session,) = list((data / "journal").iterdir())
        assert sorted(p.name for p in session.iterdir()) == ["1.json", "lock"]
