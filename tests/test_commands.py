"""Stage 3, end to end: the command system driven through a frontend."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from neomacs.testing import Client, RemoteError, running_engine
from neomacs.testing.headless import HeadlessUI

SETUP = """
import asyncio
from typing import Annotated
from neomacs.commands import Ask, KeyText, Prefix, RawPrefix, command
from neomacs.keymap import global_map
from neomacs.selection import Selection
log = []
"""


@pytest.fixture(scope="module")
def engine() -> Iterator[Path]:
    with running_engine() as path:
        with Client(path) as c:
            c.eval(SETUP)
        yield path


@pytest.fixture
def ui(engine: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine, rows=12, cols=60) as session:
        session.eval(
            f"_b = ed.buffers.new('test'); ed.frames[{session.frame_id}].window.show(_b); log.clear()"
        )
        yield session
        session.client.notify("ui.detach")
        session.client.eval("ed.buffers.kill(_b)")


def text(ui: HeadlessUI) -> str:
    return eval(ui.client.eval("_b.text"))  # noqa: S307 - a repr from our own engine


def wait_for(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.005)


# -- keys, motion, editing --------------------------------------------------------


def test_typing_and_motion(ui: HeadlessUI) -> None:
    ui.type("hello brave new world")
    ui.keys("C-a", "M-f", "M-f")
    assert ui.cursors() == [(1, 11)]
    ui.keys("M-b", "C-f", "<right>", "C-b")
    assert ui.cursors() == [(1, 7)]
    ui.keys("C-e", "<return>")
    ui.type("second")
    ui.keys("<home>", "C-d", "<backspace>", "C-k")
    assert text(ui) == "hello brave new world"
    ui.keys("M-<", "C-k")
    assert text(ui) == ""
    ui.keys("C-y", "C-a", "M-d", "M->", "M-<backspace>")
    assert text(ui) == " brave new "
    ui.keys("C-x", "C-q")
    assert ui.message == "C-x C-q is undefined"


def test_words_follow_the_syntax_table(ui: HeadlessUI) -> None:
    ui.type("snake_case kebab-case héllo")
    ui.keys("C-a", "M-f")
    assert ui.cursors() == [(1, 10)]  # '_' is a word character
    ui.keys("M-f")
    assert ui.cursors() == [(1, 16)]  # '-' is not
    ui.keys("M-f", "M-f")
    assert ui.cursors() == [(1, 27)]


def test_transpose_and_codepoint_delete(ui: HeadlessUI) -> None:
    ui.type("ab")
    ui.keys("C-b", "C-t")
    assert text(ui) == "ba"
    ui.eval("_b.replace(0, len(_b), 'e\\u0301')")
    ui.keys("M->")
    ui.call("delete_backward_codepoint")
    assert text(ui) == "e"  # only the accent went
    ui.eval("_b.replace(0, len(_b), 'e\\u0301')")
    ui.keys("M->", "<backspace>")
    assert text(ui) == ""  # backspace takes the whole cluster


def test_undo_restores_text_and_cursors(ui: HeadlessUI) -> None:
    ui.type("one")
    ui.keys("<return>")
    ui.type("two")
    ui.keys("C-a", "C-k")
    assert text(ui) == "one\n"
    ui.keys("M-<")
    ui.keys("C-/")  # undo the kill: the cursor comes back to where it was killed from
    assert text(ui) == "one\ntwo" and ui.cursors() == [(2, 0)]
    ui.keys("M-/")
    assert text(ui) == "one\n"
    ui.keys("C-/", "C-/")  # "two" was typed in one run: one step
    assert text(ui) == "one\n"
    ui.keys("C-/", "C-/")
    assert text(ui) == ""
    ui.keys("C-/")
    assert ui.message == "Nothing to undo"


def test_typing_is_undone_in_runs(ui: HeadlessUI) -> None:
    ui.eval("ed.options.typing_undo_limit = 5")
    try:
        ui.type("abcdefghijkl")
        ui.keys("C-/")
        assert text(ui) == "abcdefghij"  # the last run of up to five
        ui.keys("C-/")
        assert text(ui) == "abcde"
        ui.type("x")
        ui.keys("C-b")  # anything else ends the run
        ui.keys("C-f")
        ui.type("y")
        ui.keys("C-/")
        assert text(ui) == "abcdex"
    finally:
        ui.eval("ed.options.unset('typing_undo_limit')")


# -- selections and several cursors -----------------------------------------------


def test_selecting(ui: HeadlessUI) -> None:
    ui.type("alpha beta gamma")
    ui.keys("C-a", "C-<space>", "M-f", "M-f")
    assert ui.view.selections == [[1, 0, 1, 10]]
    ui.keys("C-g")  # stops selecting and collapses
    assert ui.view.selections == [[1, 10, 1, 10]] and ui.message == "Quit"
    ui.keys("C-x", "C-x")  # and the selection can be brought back
    assert ui.view.selections == [[1, 0, 1, 10]]
    ui.keys("C-w")
    assert text(ui) == " gamma" and ui.clipboard == "alpha beta"
    ui.keys("S-<right>", "S-<right>")  # shifted motion extends for that key only
    assert ui.view.selections == [[1, 0, 1, 2]]
    ui.keys("<right>")
    assert ui.view.selections == [[1, 3, 1, 3]]
    ui.keys("C-x", "h")
    assert ui.view.selections == [[1, 0, 1, 6]]
    ui.type("replaced")  # typing replaces what is selected
    assert text(ui) == "replaced"
    ui.keys("M-w")
    assert ui.message == "Nothing is selected"


def test_three_cursors_edit_three_places_and_undo_in_one_step(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'aaa\\nbbb\\nccc')")
    ui.keys("M-<", "C-M-n", "C-M-n")
    assert ui.cursors() == [(1, 0), (2, 0), (3, 0)]
    ui.type("xy ")
    assert text(ui) == "xy aaa\nxy bbb\nxy ccc"
    assert ui.cursors() == [(1, 3), (2, 3), (3, 3)]
    ui.keys("C-/")
    assert text(ui) == "aaa\nbbb\nccc" and ui.cursors() == [(1, 0), (2, 0), (3, 0)]
    ui.keys("M-/")
    assert text(ui) == "xy aaa\nxy bbb\nxy ccc" and len(ui.cursors()) == 3
    ui.keys("M-f", "<backspace>")  # motion and deletion happen at every cursor too
    assert text(ui) == "xy aa\nxy bb\nxy cc"
    ui.keys("C-g")
    assert len(ui.cursors()) == 1
    ui.keys("C-M-p")
    assert ui.cursors() == [(2, 5), (3, 5)]


def test_cursors_that_meet_are_merged(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'ab\\ncd')")
    ui.keys("M-<", "C-M-n")
    ui.keys("C-a")
    assert len(ui.cursors()) == 2
    ui.keys("M-<")  # a long move keeps only the primary
    assert ui.cursors() == [(1, 0)]
    ui.eval(f"ed.frames[{ui.frame_id}].window.selections.set([(2, 2), (3, 3)])")
    assert ui.cursors() == [(1, 2), (2, 0)]
    ui.keys("C-d")  # the first deletes the line break, which brings the second to the same place
    assert text(ui) == "abd" and ui.cursors() == [(1, 2)]


def test_kill_and_yank_with_several_cursors(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'one 1\\ntwo 2\\nthree 3')")
    ui.keys("M-<", "C-M-n", "C-M-n", "C-<space>", "M-f", "C-w")
    assert text(ui) == " 1\n 2\n 3" and ui.clipboard == "one\ntwo\nthree"
    ui.keys("C-e", "C-y")  # three pieces into three cursors: one each
    assert text(ui) == " 1one\n 2two\n 3three"
    ui.keys("C-g", "M->", "<return>", "C-y")  # into one cursor: all of them
    assert text(ui).endswith("\none\ntwo\nthree")


def test_yank_prefers_newer_text_from_the_system_clipboard(ui: HeadlessUI) -> None:
    ui.type("mine")
    ui.keys("C-a", "C-k")
    assert ui.clipboard == "mine"
    ui.clipboard = "from another program"
    ui.keys("C-y")
    assert text(ui) == "from another program"
    ui.keys("C-y")  # nothing newer now: the same entry again
    assert text(ui) == "from another program" * 2
    assert ui.client.eval("ed.kill_ring.entries[-2:]") == "[['mine'], ['from another program']]"


def test_yank_falls_back_when_the_frontend_does_not_answer(ui: HeadlessUI) -> None:
    ui.type("kept")
    ui.keys("C-a", "C-k")
    ui.eval(
        "import neomacs.clipboard as cb; _t = cb.CLIPBOARD_TIMEOUT; cb.CLIPBOARD_TIMEOUT = 0.05"
    )
    del ui.client.request_handlers["clipboard.get"]
    try:
        ui.keys("C-y")
        assert text(ui) == "kept"
    finally:
        ui.eval("cb.CLIPBOARD_TIMEOUT = _t")


def test_paste_and_committed_text(ui: HeadlessUI) -> None:
    ui.type("a")
    ui.client.notify("input.text", "日本語")
    ui.client.notify("input.paste", "pasted\ntext", "clipboard")
    ui.idle()
    assert text(ui) == "a日本語pasted\ntext"
    ui.keys("C-/")
    assert text(ui) == "a日本語"  # a paste is one undo step


def test_jump_list(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(f'line {n}' for n in range(1, 101)))")
    ui.keys("M-<", "C-n", "C-n", "M->")
    assert ui.cursors()[0][0] == 100
    ui.keys("M-,")
    assert ui.cursors() == [(3, 0)]
    ui.keys("C-M-,")
    assert ui.cursors()[0][0] == 100
    ui.call("goto_line", 42)
    assert ui.cursors() == [(42, 0)]
    ui.keys("M-,")
    assert ui.cursors()[0][0] == 100


# -- prefix arguments ---------------------------------------------------------------


def test_prefix_arguments(ui: HeadlessUI) -> None:
    ui.type("0123456789" * 3)
    ui.keys("C-a", "C-u", "8", "C-f")
    assert ui.cursors() == [(1, 8)]
    ui.keys("C-u", "C-f")  # a bare C-u is four
    assert ui.cursors() == [(1, 12)]
    ui.keys("C-u", "C-u", "C-f")  # two are sixteen
    assert ui.cursors() == [(1, 28)]
    ui.keys("M-5", "C-b")
    assert ui.cursors() == [(1, 23)]
    ui.keys("C-u", "1", "2", "C-b")
    assert ui.cursors() == [(1, 11)]
    ui.keys("M--", "3", "C-b")  # a negative argument reverses
    assert ui.cursors() == [(1, 14)]
    ui.keys("C-u", "3", "x")  # the argument reaches self-insert as a count
    assert text(ui)[14:17] == "xxx"
    ui.keys("C-f")  # and is gone afterwards
    assert ui.cursors() == [(1, 18)]


def test_prefix_is_shown_and_cleared(ui: HeadlessUI) -> None:
    ui.type("abcdefgh")
    ui.keys("C-a", "C-u", "4")
    assert ui.model.keys == "C-u 4-"
    ui.keys("C-g")
    assert ui.model.keys == "" and ui.client.eval(f"ed.frames[{ui.frame_id}].prefix") == "None"
    ui.keys("C-f")
    assert ui.cursors() == [(1, 1)]
    ui.keys("C-u", "5", "C-x", "C-q")  # an unbound key also clears it
    ui.keys("C-f")
    assert ui.cursors() == [(1, 2)]


def test_raw_prefix_and_per_selection_sharing(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.raw')\n"
        "def _raw(sel: Selection, raw: Annotated[object, RawPrefix] = None,\n"
        "         n: Annotated[int, Prefix(default=7)] = 7):\n"
        "    log.append((raw.universal if raw else None, raw.explicit if raw else None, n))\n"
        "global_map.bind('<f5>', 't.raw')"
    )
    ui.eval("_b.insert(0, 'a\\nb')")
    ui.keys("M-<", "C-M-n", "<f5>")
    assert ui.client.eval("log") == "[(None, None, 7), (None, None, 7)]"
    ui.eval("log.clear()")
    ui.keys("C-u", "<f5>")
    assert ui.client.eval("log") == "[(1, False, 4), (1, False, 4)]"  # resolved once, shared
    ui.eval("log.clear()")
    ui.keys("C-u", "9", "<f5>")
    assert ui.client.eval("log") == "[(1, True, 9), (1, True, 9)]"
    assert ui.client.eval(f"ed.frames[{ui.frame_id}].last_command[0]") == "'t.raw'"


def test_pending_key_prefix_is_hinted_after_a_delay(ui: HeadlessUI) -> None:
    ui.eval("ed.options.key_hint_delay = 0.05")
    try:
        ui.client.notify("input.key", "C-x")
        ui.idle()
        assert ui.model.keys == ""
        wait_for(lambda: ui.model.keys == "C-x-")
        ui.keys("C-g")
        assert ui.model.keys == "" and ui.message == "C-x C-g is undefined"
    finally:
        ui.eval("ed.options.unset('key_hint_delay')")


# -- the lane, cancellation, background work ----------------------------------------


def test_a_slow_command_keeps_the_display_live_and_typed_ahead_keys_in_order(
    ui: HeadlessUI,
) -> None:
    ui.eval(
        "@command(name='t.slow')\n"
        "async def _slow(sel: Selection):\n"
        "    log.append('start')\n"
        "    await asyncio.sleep(0.4)\n"
        "    sel.insert('S')\n"
        "global_map.bind('<f6>', 't.slow')"
    )
    ui.client.notify("input.key", "<f6>")
    for ch in "ab":
        ui.client.notify("input.key", ch)
    wait_for(lambda: ui.client.eval("log") == "['start']")
    # While it waits: the keys typed after it have not run...
    assert text(ui) == ""
    # ...but the engine is responsive and the display is live.
    batches = ui.batches
    ui.client.eval("_b.insert(0, '>')")
    wait_for(lambda: ui.text_rows_shown() == [" 1 >"])
    assert ui.batches > batches
    ui.idle()
    assert text(ui) == ">Sab"  # the slow command first, then the keys, in order


def test_c_g_cancels_a_waiting_command_and_discards_typed_ahead(ui: HeadlessUI) -> None:
    ui.client.notify("input.key", "<f6>")
    ui.client.notify("input.key", "z")
    wait_for(lambda: ui.client.eval("log") == "['start']")
    ui.client.notify("input.key", "C-g")
    ui.client.notify("input.interrupt")
    ui.idle()
    assert text(ui) == "" and ui.message == "Quit"
    ui.type("ok")  # and the editor carries on
    assert text(ui) == "ok"


def test_a_command_that_never_awaits_is_interrupted(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.spin')\n"
        "def _spin():\n"
        "    log.append('spinning')\n"
        "    while True:\n"
        "        pass\n"
        "global_map.bind('<f7>', 't.spin')"
    )
    ui.client.notify("input.key", "<f7>")
    time.sleep(0.15)  # the loop is now stuck inside the command
    started = time.monotonic()
    ui.client.notify("input.key", "C-g")
    ui.client.notify("input.interrupt")
    ui.idle(timeout=5)
    assert time.monotonic() - started < 2.0
    assert ui.client.eval("log") == "['spinning']" and ui.message == "Quit"
    ui.type("alive")
    assert text(ui) == "alive"


def test_an_error_in_a_command_is_reported_not_fatal(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.broken')\n"
        "def _broken():\n"
        "    raise ValueError('no good')\n"
        "global_map.bind('<f8>', 't.broken')"
    )
    ui.keys("<f8>")
    assert ui.message == "Error in t.broken: ValueError: no good"
    assert "Traceback" in ui.client.eval("ed.messages[-1]")
    assert "ValueError: no good" in ui.client.eval("ed.buffers.get('*messages*').text")
    ui.type("fine")
    assert text(ui) == "fine"


def test_background_commands_and_tasks(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.bg', background=True)\n"
        "async def _bg():\n"
        "    log.append(('bg', ed.frame.id, ed.buffer.name))\n"
        "    await asyncio.sleep(0.3)\n"
        "    log.append(('bg done', ed.buffer.name))\n"
        "global_map.bind('<f9>', 't.bg')"
    )
    ui.client.notify("input.key", "<f9>")
    ui.type("typed at once")  # a background command does not hold the lane
    assert text(ui) == "typed at once"
    assert ui.client.eval("[t.name for t in ed.tasks]") == "['t.bg']"
    ui.eval(f"_o = ed.buffers.new('elsewhere'); ed.frames[{ui.frame_id}].window.show(_o)")
    wait_for(lambda: ui.client.eval("ed.tasks") == "[]")
    # It kept its frame, and looked the buffer up afresh each time.
    assert ui.client.eval("log") == (f"[('bg', {ui.frame_id}, 'test'), ('bg done', 'elsewhere')]")
    ui.eval(f"ed.frames[{ui.frame_id}].window.show(_b); ed.buffers.kill(_o)")
    ui.client.notify("input.key", "<f9>")
    wait_for(lambda: ui.client.eval("len(ed.tasks)") == "1")
    ui.eval("ed.tasks[0].cancel()")
    wait_for(lambda: ui.client.eval("ed.tasks") == "[]")


def test_read_key_and_calling_commands_from_commands(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.ask_key')\n"
        "async def _ask_key():\n"
        "    key = await ed.read_key()\n"
        "    log.append(key)\n"
        "    await ed.call('self_insert', key)\n"
        "    await ed.call('forward_char', -1)\n"
        "global_map.bind('<f10>', 't.ask_key')"
    )
    ui.keys("<f10>", "q")
    assert ui.client.eval("log") == "['q']" and text(ui) == "q" and ui.cursors() == [(1, 0)]
    assert ui.call("forward_char") is None
    assert ui.cursors() == [(1, 1)]
    with pytest.raises(RemoteError, match="no such command"):
        ui.client.call("engine.call", "nope")


def test_context_pins_and_using(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.ctx')\n"
        "async def _ctx():\n"
        "    before = (ed.buffer.name, ed.view is ed.window)\n"
        "    other = ed.buffers.new('ctx-other')\n"
        "    ed.frame.window.show(other)\n"  # the user, or something, changes what is shown
        "    await asyncio.sleep(0)\n"
        "    log.append((before, ed.view.buffer.name))\n"
        "    ed.frame.window.show(_b)\n"
        "    ed.buffers.kill(other)\n"
    )
    ui.call("t.ctx")
    assert (
        ui.client.eval("log") == "[(('test', True), 'ctx-other')]"
    )  # the view is pinned, not its buffer
    ui.eval("log.clear()")
    ui.eval(
        f"_w = ed.frames[{ui.frame_id}].window\n"
        "with ed.using(_w):\n"
        "    log.append((ed.frame.id, ed.view is _w, ed.buffer is _b))"
    )
    assert ui.client.eval("log") == f"[({ui.frame_id}, True, True)]"


def test_without_a_frame() -> None:
    with running_engine() as path, Client(path) as client:
        assert client.eval("ed.frame") == "None"
        client.eval("ed.message('to nobody')")  # recorded all the same
        assert client.eval("ed.messages[-1]") == "'to nobody'"
        assert "to nobody" in client.eval("ed.buffers.get('*messages*').text")
        with pytest.raises(RemoteError, match="NoFrame"):
            client.eval("ed.view")
        client.eval(
            "import asyncio\n_r = []\n"
            "async def _ask():\n"
            "    try:\n"
            "        await ed.minibuffer.read('? ')\n"
            "    except Exception as exc:\n"
            "        _r.append(type(exc).__name__)\n"
            "ed.start_task(_ask())"
        )
        wait_for(lambda: client.eval("_r") == "['NoFrame']")
        with pytest.raises(RemoteError, match="no frame"):
            client.call("engine.call", "forward_char")


# -- minibuffers, completion, history ------------------------------------------------


def test_prompt_with_completion(ui: HeadlessUI) -> None:
    ui.keys("M-x")
    assert ui.prompt == "M-x " and len(ui.candidates()) > 20
    assert ui.model.focus != ui.view.id  # the prompt has the keyboard
    ui.type("forw wor")
    assert ui.prompt == "M-x forw wor" and ui.candidates() == ["forward_word"]
    ui.keys("<tab>")
    assert ui.prompt == "M-x forward_word"
    ui.keys("C-a", "C-k")  # ordinary editing commands work in a minibuffer
    assert ui.prompt == "M-x "
    ui.type("buffer_")
    assert ui.candidates() == ["buffer_end", "buffer_start"]
    ui.keys("<down>", "<return>")  # submitting takes the highlighted candidate
    assert ui.prompt is None and ui.model.popup is None and ui.model.focus == ui.view.id
    assert ui.client.eval(f"ed.frames[{ui.frame_id}].last_command[0]") == "'execute_command'"


def test_prompt_inside_a_prompt_and_cancelling(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(str(n) for n in range(1, 51)))")
    ui.keys("M-x")
    ui.type("goto_line")
    ui.keys("<return>")  # the command it ran asks its own question
    assert ui.prompt == "Go to line: "
    ui.type("30")
    ui.keys("<return>")
    assert ui.prompt is None and ui.cursors() == [(30, 0)]
    ui.keys("M-x")
    ui.type("goto_line")
    ui.keys("<return>")
    ui.type("7")
    ui.keys("C-g")
    assert ui.prompt is None and ui.message == "Quit" and ui.cursors() == [(30, 0)]
    ui.keys("M-x")
    ui.type("goto_line")
    ui.keys("<return>")
    ui.type("nonsense")
    ui.keys("<return>")
    assert ui.message == "not a number: nonsense" and ui.cursors() == [(30, 0)]


def test_history(ui: HeadlessUI) -> None:
    for line in ("3", "5"):
        ui.keys("M-g", "g")
        ui.type(line)
        ui.keys("<return>")
    ui.eval("_b.insert(0, 'a\\nb\\nc\\nd\\ne\\nf')")
    ui.eval(
        "@command(name='t.named')\n"
        "async def _named(name: Annotated[str, Ask.string('Name: ', history='t-names')]):\n"
        "    log.append(name)\n"
    )
    for name in ("ada", "grace"):
        ui.client.notify("input.key", "M-x")
        ui.idle()
        ui.type("t.named")
        ui.keys("<return>")
        ui.type(name)
        ui.keys("<return>")
    ui.keys("M-x")
    ui.type("t.named")
    ui.keys("<return>", "M-p")
    assert ui.prompt == "Name: grace"
    ui.keys("M-p")
    assert ui.prompt == "Name: ada"
    ui.keys("M-p", "M-n")
    assert ui.prompt == "Name: grace"
    ui.keys("M-n")
    assert ui.prompt == "Name: "
    ui.keys("M-p", "<return>")
    assert ui.client.eval("log") == "['ada', 'grace', 'grace']"
    assert ui.client.eval("ed.histories['t-names']") == "['ada', 'grace']"  # no adjacent repeats


def test_a_minibuffer_edits_like_a_buffer_but_never_holds_a_newline(ui: HeadlessUI) -> None:
    keys = [
        "h",
        "e",
        "l",
        "l",
        "o",
        "<space>",
        "w",
        "C-a",
        "M-f",
        "C-k",
        "C-y",
        "C-y",
        "C-b",
        "C-t",
    ]
    ui.keys(*keys)
    in_buffer = text(ui)
    ui.eval(
        "_got = []\nasync def _read():\n    _got.append(await ed.minibuffer.read('> '))\ned.start_task(_read())"
    )
    assert ui.prompt == "> "
    ui.keys(*keys)
    assert ui.client.eval(f"ed.frames[{ui.frame_id}].focus.buffer.text") == repr(in_buffer)
    assert ui.prompt == ("> " + in_buffer).rstrip()  # the same keys give the same text
    ui.client.notify("input.paste", "two\nlines", "clipboard")
    ui.idle()
    assert "\n" not in (ui.prompt or "") and (ui.prompt or "").endswith("two lines")
    ui.keys("C-/")  # undo works there too
    assert ui.prompt == ("> " + in_buffer).rstrip()
    ui.keys("C-M-n")  # as do several cursors, though there is only one line to put them on
    ui.keys("<return>")
    assert ui.client.eval("_got") == repr([in_buffer])


def test_multiline_and_masked_minibuffers(ui: HeadlessUI) -> None:
    ui.eval(
        "_got = []\n"
        "async def _read():\n"
        "    _got.append(await ed.minibuffer.read('msg: ', multiline=True, initial='first'))\n"
        "ed.start_task(_read())"
    )
    assert ui.prompt == "msg: first"
    ui.keys("<return>")
    ui.type("second")
    assert ui.bottom_rows() == ["msg: first", "     second"]
    assert ui.model.prompt is not None and ui.model.prompt[1]["multiline"] is True
    ui.keys("C-<return>")
    assert ui.client.eval("_got") == "['first\\nsecond']" and ui.prompt is None
    ui.eval("ed.start_task(ed.minibuffer.read('pw: ', masked=True))")
    assert ui.model.prompt is not None and ui.model.prompt[1]["masked"] is True
    ui.keys("C-g")


def test_inline_minibuffer_moves_with_the_text(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.minibuffer import Minibuffer\n"
        "_b.insert(0, 'Search: \\nresults below')\n"
        f"_f = ed.frames[{ui.frame_id}]\n"
        "_mb = Minibuffer(_f, placeholder='type here')\n"
        "_mb.on_submit.add(lambda text: log.append(text))\n"
        "_f.window.embed(_mb, 8)"
    )
    runs = ui.view.lines[0][1]
    assert runs[0][:2] == [0, "Search: "] and runs[1] == [
        8,
        {"minibuffer": int(ui.client.eval("_mb.view.id")), "covers": 0},
    ]
    assert ui.text_rows_shown()[0] == " 1 Search: ▯"
    ui.eval("_b.insert(0, 'new first line\\n')")
    assert ui.view.lines[1][1][1][0] == 8  # it went down a line with its text
    ui.eval("_f.focus_view(_mb.view)")
    ui.type("needle")
    ui.keys("<return>")
    ui.type("again")
    ui.keys("<return>")
    assert (
        ui.client.eval("log") == "['needle', 'needleagain']"
    )  # persistent: it stays and can be resubmitted
    ui.eval("_mb.close()")
    assert ui.text_rows_shown()[1] == " 2 Search:" and ui.model.focus == ui.view.id


def test_a_stale_completion_is_cancelled(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.completion import Candidate\n"
        "class _Slow:\n"
        "    async def candidates(self, text, pos):\n"
        "        log.append('asked ' + text)\n"
        "        try:\n"
        "            await asyncio.sleep(0.2)\n"
        "        except asyncio.CancelledError:\n"
        "            log.append('cancelled ' + text)\n"
        "            raise\n"
        "        return [Candidate(text + '!')]\n"
        "ed.start_task(ed.minibuffer.read('slow: ', completer=_Slow()))"
    )
    for ch in "ab":
        ui.client.notify("input.key", ch)
    wait_for(lambda: ui.candidates() == ["ab!"])
    assert ui.client.eval("log") == "['asked ', 'cancelled ', 'asked a', 'cancelled a', 'asked ab']"
    ui.keys("C-g")


# -- files, buffers, messages, the rest of the protocol -------------------------------


def test_files_and_buffers(ui: HeadlessUI, tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("from disk\n")
    (tmp_path / "other.txt").write_text("")
    ui.keys("C-x", "C-f")
    assert ui.prompt == "Find file: "
    ui.type(f"{tmp_path}/no")
    assert ui.candidates() == [f"{tmp_path}/notes.txt"]
    ui.keys("<tab>", "<return>")
    assert ui.text_rows_shown() == [" 1 from disk", " 2"] and ui.view.info["name"] == "notes.txt"
    ui.type("edited ")
    ui.keys("C-x", "C-s")
    assert (tmp_path / "notes.txt").read_text() == "edited from disk\n"
    assert ui.message.startswith("Wrote ") and ui.modeline.startswith(" - notes.txt")
    ui.keys("C-x", "b")
    ui.type("test")
    assert ui.candidates()[0] == "test"
    ui.keys("<return>")
    assert ui.view.info["name"] == "test"
    ui.type("unsaved")
    ui.keys("C-x", "C-s")  # a buffer with no file asks for one
    assert ui.prompt == "Write file: "
    ui.type(str(tmp_path / "fresh.txt"))
    ui.keys("<return>")
    assert (tmp_path / "fresh.txt").read_text() == "unsaved"
    ui.eval("_b.file = None")
    ui.call("buffers.switch", "notes.txt")
    ui.keys("C-x", "k")
    assert ui.view.info["name"] != "notes.txt"
    assert ui.client.eval("ed.buffers.get('notes.txt')") == "None"
    ui.call("buffers.switch", "test")


def test_views_remember_where_they_were_in_each_buffer(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, '\\n'.join(str(n) for n in range(1, 201)))")
    ui.call("goto_line", 150)
    ui.call("buffers.switch", "*scratch*")
    assert ui.cursors() == [(1, 0)]
    ui.call("buffers.switch", "test")
    assert ui.cursors() == [(150, 0)]
    assert any(row.startswith("150 ") for row in ui.text_rows_shown())  # and it is in view


def test_mouse(ui: HeadlessUI) -> None:
    ui.eval("_b.insert(0, 'click here\\nsecond line of text')")
    ui.mouse(2, 7)
    assert ui.view.selections == [[2, 7, 2, 7]]
    ui.mouse(2, 14, action="drag")
    assert ui.view.selections == [[2, 7, 2, 14]]
    ui.mouse(2, 14, action="release")  # unbound, and silently so
    assert ui.message != "<up-mouse-1> is undefined"
    ui.mouse(1, 2, mods="M")
    assert ui.cursors() == [(1, 2), (2, 14)] or ui.cursors() == [(1, 2), (2, 7)]
    ui.mouse(1, 7, clicks=2)
    assert ui.view.selections == [[1, 6, 1, 10]]  # the word
    ui.mouse(2, 3, clicks=3)
    assert ui.view.selections == [[2, 0, 2, 19]]  # the line
    before = ui.message
    ui.mouse(1, 0, area="gutter:numbers")  # <gutter-mouse-1> has no binding yet
    assert ui.message == before


def test_dropped_files_are_visited(ui: HeadlessUI, tmp_path: Path) -> None:
    path = tmp_path / "dropped.txt"
    path.write_text("dropped in")
    ui.client.notify("input.drop", [str(path)], ui.view.id, 1, 0)
    ui.idle()
    assert ui.text_rows_shown() == [" 1 dropped in"]
    ui.call("buffers.kill")


def test_messages_menu_and_new_frame(ui: HeadlessUI) -> None:
    ui.eval("ed.message('hello there')")
    assert ui.message == "hello there" and ui.screen()[-1] == "hello there"
    ui.eval("ed.message('careful', kind='warning'); ed.message('quiet', kind='log')")
    assert ui.model.messages[-1] == ("careful", "warning")  # a log message is recorded, not shown
    assert ui.client.eval("ed.messages[-1]") == "'quiet'"
    menu = {item["label"]: item for item in ui.model.menu}
    assert list(menu) == ["File", "Edit", "Tools"]
    opened = menu["File"]["children"][0]
    assert opened == {"label": "Open…", "command": "files.find", "key": "s-o", "enabled": True}
    ui.eval("ed.menu.add('Tools/Mine', 'forward_word')")
    assert ui.model.menu[-1]["children"][-1] == {
        "label": "Mine", "command": "forward_word", "key": "M-f", "enabled": True,
    }  # fmt: skip
    ui.eval("ed.menu.remove('Tools/Mine')")
    ui.keys("C-x", "5", "2")
    wait_for(lambda: ui.open_frame_requests == 1)


def test_show_by_role(ui: HeadlessUI) -> None:
    ui.eval("_h = ed.buffers.new('*help*', 'help text'); _v = ed.show(_h, role='help')")
    assert ui.text_rows_shown() == [" 1 help text"]  # every role is in the main area for now
    assert ui.client.eval("_v is ed.window") == "True"
    ui.eval("ed.show(_b, focus=True); ed.buffers.kill(_h)")


def test_definitions_in_evaluated_code_are_owned_by_its_module(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.ownership import ownership\n"
        "@command(name='t.temporary')\n"
        "def _temporary(): ...\n"
        "global_map.bind('<f12>', 't.temporary')"
    )
    owned = ui.client.eval("sorted(k for k, _ in ownership.owned('neomacs_scratch'))")
    assert "'command'" in owned and "'key'" in owned
    assert ui.client.eval("ownership.owner_of('command', 't.temporary')") == "'neomacs_scratch'"
    assert (
        ui.client.eval("ownership.owner_of('command', 'forward_word')") == "'neomacs.std.movement'"
    )


def test_redefining_a_command_takes_effect_on_the_next_key(ui: HeadlessUI) -> None:
    ui.eval(
        "@command(name='t.live')\ndef _live(sel: Selection):\n    sel.insert('old')\nglobal_map.bind('<f11>', 't.live')"
    )
    ui.keys("<f11>")
    ui.eval("@command(name='t.live')\ndef _live(sel: Selection):\n    sel.insert(' new')")
    ui.keys("<f11>")  # the binding names the command, so nothing needed rebinding
    assert text(ui) == "old new"


def test_two_frames_have_their_own_cursors_prefixes_and_prompts(engine: Path) -> None:
    with HeadlessUI(engine, rows=10, cols=40) as a, HeadlessUI(engine, rows=10, cols=40) as b:
        a.eval(f"_s = ed.buffers.new('shared', 'shared text'); ed.frames[{a.frame_id}].window.show(_s); "
               f"ed.frames[{b.frame_id}].window.show(_s)")  # fmt: skip
        a.keys("C-u", "3")
        b.keys("C-f")  # b's key does not consume a's pending prefix
        b.idle()
        assert b.cursors() == [(1, 1)]
        a.keys("C-f")
        b.idle()
        assert a.cursors() == [(1, 3)] and b.cursors() == [(1, 1)]
        a.keys("M-x")
        b.idle()
        assert a.prompt == "M-x " and b.prompt is None
        b.type("B")  # b types into the buffer while a's prompt waits
        a.idle()
        assert a.text_rows_shown() == [" 1 sBhared text"] and a.prompt == "M-x "
        a.keys("C-g")
        for session in (a, b):
            session.client.notify("ui.detach")
        a.client.eval("ed.buffers.kill(_s)")


def test_command_hooks_and_ui_attached(engine: Path) -> None:
    with Client(engine) as probe:
        probe.eval(
            "from neomacs import hooks\n"
            "def _pre(cmd): log.append('pre ' + cmd.name)\n"
            "async def _post(cmd):\n"
            "    await asyncio.sleep(0)\n"
            "    log.append('post ' + cmd.name)\n"
            "def _attached(frame): log.append('attached')\n"
            "hooks.pre_command.add(_pre); hooks.post_command.add(_post); hooks.ui_attached.add(_attached)"
        )
        try:
            with HeadlessUI(engine, rows=8, cols=30) as ui:
                assert probe.eval("log") == "['attached']"
                ui.keys("C-f")
                assert probe.eval("log") == "['attached', 'pre forward_char', 'post forward_char']"
                ui.client.notify("ui.detach")
        finally:
            probe.eval(
                "hooks.pre_command.remove(_pre); hooks.post_command.remove(_post); "
                "hooks.ui_attached.remove(_attached); log.clear()"
            )


def test_named_places_fall_back_to_the_prompt_area(engine: Path) -> None:
    with HeadlessUI(engine, rows=8, cols=40, attach={"places": ["search-bar"]}) as ui:
        ui.eval(
            "from neomacs.minibuffer import Minibuffer\n"
            f"_f = ed.frames[{ui.frame_id}]\n"
            "_m1 = Minibuffer(_f, 'find: '); _f.place(_m1, 'search-bar')\n"
        )
        view_id = int(ui.client.eval("_m1.view.id"))
        assert ui.model.minibuffers[view_id][0] == {"kind": "place", "name": "search-bar"}
        assert ui.prompt is None  # it is somewhere else, not in the prompt area
        ui.eval("_m2 = Minibuffer(_f, 'other: '); _f.place(_m2, 'no-such-place')")
        assert ui.prompt == "other: "  # an unknown place: the prompt area instead
        ui.eval("_m1.close(); _m2.close()")
        assert ui.model.minibuffers == {} and ui.prompt is None
        ui.client.notify("ui.detach")


def test_deprecation_warnings_reach_the_user(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.stability import deprecated\n"
        "@deprecated('use newer', since='0.3', removal='1.0')\n"
        "def _older(): return 1\n"
        "_older(); _older()"
    )
    assert ui.model.messages[-1][1] == "warning"
    assert "deprecated since 0.3 and to be removed in 1.0: use newer" in ui.message
    assert ui.client.eval("sum('deprecated since 0.3' in m for m in ed.messages)") == "1"
    ui.eval("from neomacs import commands as _c; _c.alias('t.old_name', 'forward_char')")
    ui.call("t.old_name")
    assert ui.cursors() == [(1, 0)] and "renamed to forward_char" in ui.message


def test_benchmark_runs() -> None:
    from neomacs.tools import bench

    results = bench.run(40)
    assert results["highlighted_runs_on_typed_line"] > 1
    for name in ("self_insert", "three_cursors", "with_simulated_highlighting"):
        assert results[name]["engine"]["n"] == 40 and results[name]["engine"]["p50"] > 0
    assert results["burst"]["batches"] <= 40
