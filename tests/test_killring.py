"""The kill ring and its chooser (KILLRING.md)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing.headless import HeadlessUI


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.01)


def ring(ui: HeadlessUI) -> list[str]:
    """The ring's texts, newest first, as the chooser lists them."""
    return value(ui, "[e.text for e in reversed(ed.kill_ring.entries)]")


def start(session: HeadlessUI, text: str = "") -> None:
    session.eval(
        f"_w = ed.frames[{session.frame_id}].window; _b = ed.buffers.new('work'); _w.show(_b); "
        f"_b.insert(0, {text!r}); _w.selections.set([(0, 0)]); "
        "ed.kill_ring.entries.clear(); _w.frame.last_clipboard = ''"
    )
    session.clipboard = ""


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=16, cols=70) as session:
        start(session)
        yield session
        session.client.notify("ui.detach")


def kills(ui: HeadlessUI, *texts: str) -> None:
    for text in texts:
        ui.eval(f"ed.kill_ring.push([{text!r}], ed.frames[{ui.frame_id}])")


# -- the ring ---------------------------------------------------------------------------


def test_every_kill_is_its_own_entry_and_goes_to_the_system_clipboard(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'one\\ntwo\\nthree\\n'); _w.selections.set([(0, 0)])")
    ui.keys("C-k", "C-k", "C-k", "C-k")  # the text, the line break, the next text, its break
    # Not merged.  The line break was killed twice and is there once, moved to the top.
    assert ring(ui) == ["\n", "two", "one"]
    assert ui.clipboard == "\n"
    ui.keys("C-<space>", "C-e", "M-w")
    assert ring(ui)[0] == "three" and ui.clipboard == "three"
    assert value(ui, "[e.source for e in ed.kill_ring.entries][-2:]") == ["kill", "copy"]


def test_the_ring_keeps_only_so_many(ui: HeadlessUI) -> None:
    ui.eval("ed.options.kill_ring_size = 3")
    try:
        kills(ui, "a", "b", "c", "d")
        assert ring(ui) == ["d", "c", "b"]
    finally:
        ui.eval("ed.options.unset('kill_ring_size')")


# -- the system clipboard, coming in ---------------------------------------------------------


def test_what_was_copied_elsewhere_is_captured_when_the_window_regains_focus(
    ui: HeadlessUI,
) -> None:
    kills(ui, "killed before")
    ui.clipboard = "copied in a browser"
    ui.client.notify("input.focus", True)
    wait_for(lambda: len(ring(ui)) == 2)
    # A kill made now would once have lost the browser's text: it replaces it
    # on the clipboard before any yank looks.
    kills(ui, "killed after")
    assert ring(ui) == ["killed after", "copied in a browser", "killed before"]
    assert value(ui, "ed.kill_ring.entries[1].source") == "system"
    # Coming back again with nothing new adds nothing.
    ui.client.notify("input.focus", True)
    ui.client.notify("input.focus", False)
    ui.idle()
    time.sleep(0.1)
    assert len(ring(ui)) == 3
    # The same text copied elsewhere again is the same entry, moved up.
    ui.clipboard = "killed before"
    ui.client.notify("input.focus", True)
    wait_for(lambda: ring(ui)[0] == "killed before")
    assert len(ring(ui)) == 3


def test_text_too_long_to_keep_is_pasted_and_not_kept(ui: HeadlessUI) -> None:
    ui.eval("ed.options.kill_ring_entry_limit = 10")
    try:
        kills(ui, "small")
        ui.clipboard = "far too long to keep in the ring"
        ui.keys("C-y")
        assert value(ui, "_b.text") == "far too long to keep in the ring"
        assert ring(ui) == ["small"]
        # One cut here is kept whatever its length: the user would lose it otherwise.
        ui.keys("C-a", "C-k")
        assert ring(ui)[0] == "far too long to keep in the ring"
    finally:
        ui.eval("ed.options.unset('kill_ring_entry_limit')")


def test_a_frontend_that_does_not_answer_holds_nothing_up_on_focus(ui: HeadlessUI) -> None:
    del ui.client.request_handlers["clipboard.get"]
    began = time.monotonic()
    ui.client.notify("input.focus", True)
    ui.type("typed at once")
    assert value(ui, "_b.text") == "typed at once" and time.monotonic() - began < 0.4


def test_text_too_long_to_keep_is_still_pasted_after_the_window_was_focused(
    ui: HeadlessUI,
) -> None:
    ui.eval("ed.options.kill_ring_entry_limit = 10")
    try:
        kills(ui, "small")
        ui.clipboard = "far too long to keep in the ring"
        # Seen first when the window regains focus, and not kept.
        ui.client.notify("input.focus", True)
        wait_for(lambda: value(ui, "_w.frame.last_clipboard") == ui.clipboard)
        assert ring(ui) == ["small"]
        ui.keys("C-y", "C-y")  # it is what the clipboard holds, each time it is asked for
        assert value(ui, "_b.text") == "far too long to keep in the ring" * 2
        # Until something else is copied, here or elsewhere.
        kills(ui, "newer")
        ui.keys("C-y")
        assert value(ui, "_b.text").endswith("ringnewer")
    finally:
        ui.eval("ed.options.unset('kill_ring_entry_limit')")


# -- M-y ----------------------------------------------------------------------------------------


def rows(ui: HeadlessUI) -> list[tuple[str, str]]:
    popup = ui.model.popup
    return [(item[0], item[1]) for item in popup["items"]] if popup else []


def preview(ui: HeadlessUI) -> str:
    return value(ui, "ed.buffers.get('*kill ring*').text")


def test_the_chooser_lists_the_ring_and_previews_the_highlighted_entry(ui: HeadlessUI) -> None:
    kills(ui, "oldest", "def f(a, b):\n    return a + b\n", "\n\n  indented first line\nmore\n")
    ui.eval(f"ed.kill_ring.push(['p1', 'p2', 'p3'], ed.frames[{ui.frame_id}])")
    ui.clipboard = "copied elsewhere since"
    ui.keys("M-y")
    assert ui.prompt == "Yank: "
    # Newest first, the clipboard's news among them; each row a line and a note.
    assert rows(ui) == [
        ("copied elsewhere since", ""),
        ("p1", "3 pieces"),
        ("indented first line", "4 lines"),
        ("def f(a, b):", "2 lines"),
        ("oldest", ""),
    ]
    assert preview(ui) == "copied elsewhere since"
    ui.keys("<down>")
    assert preview(ui) == "p1\n" + "─" * 24 + "\np2\n" + "─" * 24 + "\np3"
    ui.keys("M-y", "M-y")  # the key that opened it walks the ring
    assert preview(ui) == "def f(a, b):\n    return a + b\n"
    # The preview is on show under the text, and the keyboard is still in the prompt.
    shown = ui.view_showing("*kill ring*")
    assert shown is not None and ui.lines_of(shown)[:2] == ["def f(a, b):", "    return a + b"]
    assert value(ui, "ed.frame.focus.buffer.name") == "*minibuffer*"
    ui.keys("<return>")
    assert value(ui, "_b.text") == "def f(a, b):\n    return a + b\n"
    assert ui.clipboard == "def f(a, b):\n    return a + b\n"
    assert ring(ui)[0] == "def f(a, b):\n    return a + b\n" and len(ring(ui)) == 5
    # Everything of the chooser has gone.
    assert ui.prompt is None and ui.view_showing("*kill ring*") is None
    assert value(ui, "(ed.frame.focus is _w, [p.name for p in ed.frame.panels.values() if p.visible])") == (
        True, [],
    )  # fmt: skip
    # And the next C-y, here or anywhere, is what was chosen.
    ui.keys("C-y")
    assert value(ui, "_b.text") == "def f(a, b):\n    return a + b\n" * 2


def test_typing_narrows_the_list_by_text_anywhere_in_an_entry(ui: HeadlessUI) -> None:
    kills(ui, "alpha\nNeedle on the second line\nomega", "alpha\nsomething else\nomega", "plain")
    ui.keys("M-y")
    assert [r[0] for r in rows(ui)] == ["plain", "alpha", "alpha"]  # two that read the same
    ui.type("needle")
    # Found on a later line; the row shows that line.  Case is ignored.
    assert rows(ui) == [("Needle on the second line", "3 lines")]
    assert preview(ui) == "alpha\nNeedle on the second line\nomega"
    ui.keys(
        "<backspace>", "<backspace>", "<backspace>", "<backspace>", "<backspace>", "<backspace>"
    )
    ui.type("Needle")  # a capital: now case matters, and still found
    assert len(rows(ui)) == 1
    ui.keys("C-a", "C-k")
    ui.type("NEEDLE")
    assert rows(ui) == [] and preview(ui) == ""
    ui.keys("C-a", "C-k")
    # Two entries with the same first line are different things, and are told apart.
    ui.type("alpha")
    assert [r[0] for r in rows(ui)] == ["alpha", "alpha"]
    ui.keys("<down>", "<return>")
    assert value(ui, "_b.text") == "alpha\nNeedle on the second line\nomega"


def test_straight_after_a_yank_the_choice_replaces_what_was_yanked(ui: HeadlessUI) -> None:
    kills(ui, "first", "second", "third")
    ui.eval("_b.insert(0, '[]'); _w.selections.set([(1, 1)])")
    ui.keys("C-y")
    assert value(ui, "_b.text") == "[third]"
    ui.keys("M-y", "<down>", "<down>", "<return>")
    assert value(ui, "_b.text") == "[first]" and value(ui, "_w.cursor.pos") == 6
    ui.keys("M-y", "<down>", "<return>")  # and again, after the one before
    assert value(ui, "_b.text") == "[third]"
    ui.keys("C-/")  # one step
    assert value(ui, "_b.text") == "[first]"
    ui.keys("M-/")
    # After any other command it inserts, as a yank would.
    ui.keys("C-f")
    ui.keys("M-y", "<down>", "<down>", "<return>")
    assert value(ui, "_b.text") == "[third]second"
    # And after an edit to the buffer from elsewhere.
    ui.keys("C-y")
    ui.eval("_b.insert(0, '>')")
    ui.keys("M-y", "<down>", "<return>")
    assert value(ui, "_b.text") == ">[third]secondsecondthird"


def test_an_edit_made_while_the_chooser_is_open_is_not_overwritten(ui: HeadlessUI) -> None:
    kills(ui, "first", "second")
    ui.eval("_b.insert(0, 'keep this []'); _w.selections.set([(11, 11)])")
    ui.keys("C-y")
    assert value(ui, "_b.text") == "keep this [second]"
    ui.keys("M-y", "<down>")
    # Something else edits the buffer meanwhile: the yanked text is no longer where it was.
    ui.eval("_b.insert(0, 'ADDED ')")
    ui.keys("<return>")
    # Inserted at the cursor, as after any other change; nothing unrelated is replaced.
    assert value(ui, "_b.text") == "ADDED keep this [secondfirst]"


def test_the_whole_ring_can_be_walked(ui: HeadlessUI) -> None:
    kills(ui, *[f"entry {i:02d}" for i in range(60)])
    ui.keys("M-y")
    assert len(rows(ui)) == 60 and rows(ui)[-1][0] == "entry 00"
    ui.keys("<up>")  # round to the oldest
    assert preview(ui) == "entry 00"
    ui.keys("<return>")
    assert value(ui, "_b.text") == "entry 00"


def test_two_frames_each_preview_their_own_choice(ui: HeadlessUI, engine_path: Path) -> None:
    kills(ui, "older", "newer")
    with HeadlessUI(engine_path, rows=16, cols=70) as second:
        second.eval(
            f"_w2 = ed.frames[{second.frame_id}].window; _b2 = ed.buffers.new('other'); "
            "_b2.set_mode('python'); _w2.show(_b2)"
        )
        ui.keys("M-y")
        second.keys("M-y", "<down>")
        shown = [
            value(ui, f"ed.buffers.get({name!r}).text")
            for name in ("*kill ring*", "*kill ring*<2>")
        ]
        assert shown == ["newer", "older"]
        # Each in the mode of the buffer its text is going into: `work` has none.
        modes = value(
            ui,
            "[getattr(ed.buffers.get(n).mode, 'name', None) for n in ('*kill ring*', '*kill ring*<2>')]",
        )
        assert modes == [None, "python"]
        second.keys("<return>")
        ui.keys("<return>")
        assert value(ui, "(_b.text, _b2.text)") == ("newer", "older")
        assert value(ui, "[b.name for b in ed.buffers if b.name.startswith('*kill ring')]") == []
        second.client.notify("ui.detach")


def test_a_preview_sent_to_the_main_area_by_the_user_is_not_shown_there(ui: HeadlessUI) -> None:
    kills(ui, "first", "second")
    ui.eval(
        "from neomacs.panels import Main\n"
        "_p = dict(ed.options.placement); _p['preview'] = Main(); ed.options.placement = _p"
    )
    try:
        ui.keys("M-y", "<down>")
        # The window keeps the buffer the text is going into; there is no preview on show.
        assert value(ui, "_w.buffer is _b") is True and ui.view_showing("*kill ring*") is None
        ui.keys("<return>")
        assert value(ui, "(_b.text, _w.id in ed.frame.views)") == ("first", True)
    finally:
        ui.eval("ed.options.unset('placement')")


def test_cancelling_changes_nothing(ui: HeadlessUI) -> None:
    kills(ui, "first", "second")
    before = ring(ui)
    ui.keys("M-y", "<down>", "C-g")
    assert ring(ui) == before and value(ui, "_b.text") == "" and ui.clipboard == "second"
    assert ui.prompt is None and ui.view_showing("*kill ring*") is None
    ui.keys("M-y", "<down>", "<escape>")
    assert ring(ui) == before and ui.prompt is None


def test_an_entry_can_be_removed_from_the_chooser(ui: HeadlessUI) -> None:
    kills(ui, "keep", "a password, copied by mistake", "newest")
    ui.keys("M-y", "<down>", "C-d")
    assert ring(ui) == ["newest", "keep"]
    assert [r[0] for r in rows(ui)] == ["newest", "keep"] and preview(ui) == "keep"
    ui.keys("C-d", "C-d")  # the last one goes, and the chooser with it
    assert ring(ui) == [] and ui.prompt is None and ui.message == "The kill ring is empty"
    ui.keys("M-y")
    assert ui.message == "The kill ring is empty" and ui.prompt is None


def test_several_cursors(ui: HeadlessUI) -> None:
    ui.eval(f"ed.kill_ring.push(['A', 'B', 'C'], ed.frames[{ui.frame_id}])")
    kills(ui, "one")
    ui.eval("_b.insert(0, '1:\\n2:\\n3:'); _w.selections.set([(2, 2), (5, 5), (8, 8)])")
    ui.keys("C-y")
    assert value(ui, "_b.text") == "1:one\n2:one\n3:one"
    ui.keys("M-y", "<down>", "<return>")  # a piece for each cursor, in place of what was yanked
    assert value(ui, "_b.text") == "1:A\n2:B\n3:C"
    ui.keys("C-/")
    assert value(ui, "_b.text") == "1:one\n2:one\n3:one"


def test_the_preview_takes_the_mode_of_where_the_text_is_going_and_scrolls(
    ui: HeadlessUI,
) -> None:
    long = "".join(f"line {i}\n" for i in range(1, 61))
    kills(ui, long)
    ui.eval("_b.set_mode('python')")
    ui.keys("M-y")
    assert value(ui, "ed.buffers.get('*kill ring*').mode.name") == "python"
    shown = ui.view_showing("*kill ring*")
    assert shown is not None and ui.lines_of(shown)[0] == "line 1"
    top = f"ed.buffers.get('*kill ring*').line_of(ed.frame.views[{shown.id}].cursor.pos)"
    ui.keys("C-v")
    assert value(ui, top) > 1
    ui.keys("M-v")
    assert value(ui, top) == 1
    # It is not something to be recovered after a crash, or to be saved.
    assert value(ui, "(ed.buffers.get('*kill ring*').modified, ed.journal.wanted(ed.buffers.get('*kill ring*')))") == (
        False, False,
    )  # fmt: skip
    ui.keys("C-g")
    assert value(ui, "ed.buffers.get('*kill ring*')") is None  # and it goes with the chooser


def test_not_into_a_prompt(ui: HeadlessUI) -> None:
    kills(ui, "x")
    ui.keys("M-x", "M-y")
    assert ui.message == "Cannot yank from the ring into a prompt"
    ui.keys("C-g")


# -- a frontend with a place for it -------------------------------------------------------------


def test_a_frontend_with_a_place_for_the_chooser_is_given_the_preview_to_draw(
    engine_path: Path,
) -> None:
    attach = {"capabilities": ["panels"], "places": ["chooser"]}
    with HeadlessUI(engine_path, rows=16, cols=70, attach=attach) as ui:
        start(ui)
        kills(ui, "older\ntext", "newer")
        ui.keys("M-y", "<down>")
        ((box, (host, options)),) = ui.model.minibuffers.items()
        assert host == {"kind": "place", "name": "chooser"}
        assert options["label"][0][1] == "Yank: "
        view = ui.model.views[options["preview"]]
        assert ui.lines_of(view) == ["older", "text"]
        assert ui.model.focus == box and ui.model.popup["view"] == box
        # No panel is opened for it: the frontend draws the preview in the chooser.
        assert ui.panels() == {} or all(not p["visible"] for p in ui.panels().values())
        ui.keys("<return>")
        assert value(ui, "_b.text") == "older\ntext"
        assert ui.model.minibuffers == {} and options["preview"] not in value(
            ui, "list(ed.frame.views)"
        )
        ui.client.notify("ui.detach")
