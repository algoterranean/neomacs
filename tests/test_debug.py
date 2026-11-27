"""Stage 5: the debugger (ENGINE 10)."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import Client, running_engine
from neomacs.testing.client import short_tempdir
from neomacs.testing.headless import HeadlessUI

SETUP = """
import neomacs.std.movement as _mv, neomacs.std.editing as _ed_mod
from neomacs.debug import breakpoints as _bp
def _break(buffer, needle, offset=0, condition=None):
    line = next(n for n in range(1, buffer.line_count + 1) if needle in buffer.line_text(n)) + offset
    _bp.toggle(ed.debugger, buffer, line, condition)
    return line
def _clear():
    for b in ed.buffers:
        for span in list(b.layer('debug')):
            span.remove()
    _bp.refresh(ed.debugger)
    ed.options.debug_on_raise = ''
def _state():
    d = ed.debugger
    f = d.stack[d.selected] if d.stack else None
    return (d.status, f[0].f_code.co_name if f else None, f[1] if f else None)
"""


@pytest.fixture(scope="module")
def engine() -> Iterator[Path]:
    with running_engine() as path:
        with Client(path) as c:
            c.eval(SETUP)
        yield path


@pytest.fixture
def ui(engine: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine, rows=14, cols=70) as session:
        session.eval(
            f"_w = ed.frames[{session.frame_id}].window\n"
            "_t = ed.buffers.new('text', 'alpha beta gamma\\nsecond line\\nthird'); _w.show(_t)\n"
            "_s = ed.buffers.get('*scratch*'); _s.replace(0, len(_s), '')"
        )
        yield session
        if value(session, "ed.debugger.paused"):
            session.call("debug.abort")
        session.eval("ed.debugger.stop_inspecting(); _clear(); ed.buffers.kill(_t)")
        session.client.notify("ui.detach")


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def state(ui: HeadlessUI) -> tuple[str, str | None, int | None]:
    return value(ui, "_state()")


def define(ui: HeadlessUI, text: str) -> None:
    """Put TEXT in the scratch buffer and evaluate it with the real command."""
    ui.eval(f"_s.replace(0, len(_s), {text!r}); _w.show(_s)")
    ui.call("eval_buffer")
    ui.eval("_w.show(_t); _w.selections.set([(0, 0)])")


# -- the step's "done when" --------------------------------------------------------


def test_break_in_a_builtin_command_look_around_change_a_local_and_continue(ui: HeadlessUI) -> None:
    ui.eval("_f = ed.buffers.visit(_mv.__file__); _w.show(_f)")
    ui.eval("_w.selections.set([(_f.text.index('sel.forward_char(n)'),) * 2])")
    ui.keys("<f9>")  # the real command, on the line the cursor is on
    line = value(ui, "_f.line_of(_w.cursor.pos)")
    assert ui.message == f"Breakpoint at line {line}"
    assert value(ui, f"list(_w._lines({line}, {line})[0][0]['gutter'].values())[0][0]") == "●"
    ui.eval("_w.show(_t); _w.selections.set([(0, 0)])")

    ui.keys("C-f")  # the key of the command with the breakpoint
    assert state(ui) == ("paused", "forward_char", line)
    assert value(ui, "(_w.buffer.name, _w.buffer.line_of(_w.cursor.pos))") == ("movement.py", line)
    assert ui.message == f"Paused (breakpoint) at movement.py, line {line}, in forward_char"
    assert "[paused]" in ui.modeline
    assert value(ui, "len(_f.layer('debug-line'))") == 1
    assert ui.cursors() == [(line, 0)] and value(ui, "_t.text[:5]") == "alpha"

    # Look around in other buffers while it is paused: move, edit, undo, a prompt.
    ui.keys("C-x", "b")
    ui.type("text")
    ui.keys("<return>")
    assert value(ui, "_w.buffer.name") == "text"
    ui.keys("<down>", "<end>")  # <down> asks the frontend for geometry, from the debug loop
    assert ui.cursors() == [(2, 11)]
    ui.type("!")
    assert value(ui, "_t.line_text(2)") == "second line!"
    ui.keys("C-/")
    assert value(ui, "_t.line_text(2)") == "second line"
    ui.keys("C-a", "<up>")
    assert ui.cursors() == [(1, 0)]

    # Change a local in the paused frame, then let it go.
    ui.keys("M-:")
    ui.type("n = 4")
    ui.keys("<return>")
    ui.keys("M-:")
    ui.type("(n, sel.pos)")
    ui.keys("<return>")
    assert ui.message == "(4, 0)"
    # The stack and variables are in a panel beside the source, kept up to date.
    assert ui.panels()["bottom"]["visible"] and value(ui, "_w.frame.focus is _w")
    shown = value(ui, "ed.buffers.get('*debug*').text")
    assert shown.startswith(
        "Paused: breakpoint\n\nStack, innermost first:\n> forward_char  movement.py:"
    )
    assert "\n  invoke  dispatch.py:" in shown and "\n  n = 4\n" in shown
    ui.keys("<f5>")
    assert state(ui) == ("", None, None)
    assert "[paused]" not in ui.modeline
    assert value(ui, "_w.buffer.name") == "text" and ui.cursors() == [(1, 4)]  # moved by 4, not 1
    assert value(ui, "len(_f.layer('debug-line'))") == 0
    assert value(ui, "ed.debugger.pauses") >= 1

    # The breakpoint is still there; removing it lets the command run through.
    ui.keys("C-f")
    assert state(ui)[0] == "paused"
    ui.keys("<f9>")  # the paused line is where the cursor is
    assert ui.message == f"Breakpoint removed from line {line}"
    ui.keys("<f5>")
    ui.keys("C-f")
    assert state(ui)[0] == "" and ui.cursors() == [(1, 6)]


# -- stepping -----------------------------------------------------------------------

STEPS = """
log = []
def helper(x):
    y = x + 1
    return y * 2

@command
def stepper() -> None:
    a = 1
    b = helper(a)
    c = b + 1
    log.append((a, b, c))

@command
async def sleeper() -> None:
    before = 1
    got = await asyncio.sleep(0.15, "slept")
    after = before + 1
    log.append((before, got, after))

@command
async def outer_cmd() -> None:
    x = await inner(2)
    log.append(x)

async def inner(n):
    await asyncio.sleep(0)
    m = n * 10
    return m
"""


def test_step_next_out_and_the_stack(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'a = 1')")
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    assert state(ui) == ("paused", "stepper", 9)
    ui.keys("<f10>")
    assert state(ui) == ("paused", "stepper", 10)
    ui.keys("<f11>")  # into helper
    assert state(ui) == ("paused", "helper", 4)
    assert value(ui, "[f.f_code.co_name for f, _ in ed.debugger.stack][:3]") == [
        "helper",
        "stepper",
        "invoke",
    ]
    # Up the stack: the caller's line and variables; evaluation happens there.
    ui.keys("C-c", "u")
    assert state(ui) == ("paused", "stepper", 10)
    assert value(ui, "_w.buffer.line_of(_w.cursor.pos)") == 10
    ui.keys("M-:")
    ui.type("a")
    ui.keys("<return>")
    assert ui.message == "1"
    ui.keys("C-c", "d")
    ui.keys("M-:")
    ui.type("x + 100")
    ui.keys("<return>")
    assert ui.message == "101"
    ui.keys("S-<f11>")  # out of helper: back in the caller, on its next line
    assert state(ui) == ("paused", "stepper", 11)
    ui.keys("<f10>")
    assert state(ui) == ("paused", "stepper", 12)
    ui.keys("<f10>")  # the function returns: on into its caller
    assert state(ui)[:2] == ("paused", "invoke")
    ui.keys("<f5>")
    assert state(ui)[0] == "" and value(ui, "log") == [(1, 4, 5)]


def test_stepping_over_a_call_that_has_a_breakpoint_stops_there(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'b = helper(a)'); _break(_s, 'return y * 2')")
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    assert state(ui) == ("paused", "stepper", 10)
    ui.keys("<f10>")
    assert state(ui) == ("paused", "helper", 5)
    ui.keys("<f5>")
    assert value(ui, "log") == [(1, 4, 5)]


def test_stepping_across_an_await_lets_the_world_run(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'before = 1')")
    ui.eval("ed.start_task(ed.call('sleeper'))")
    ui.idle()
    assert state(ui) == ("paused", "sleeper", 16)
    ui.keys("<f10>")
    assert state(ui) == ("paused", "sleeper", 17)
    ui.client.notify("input.key", "<f10>")  # over the await: the command sleeps, unpaused
    time.sleep(0.05)
    assert value(ui, "(ed.debugger.paused, ed.debugger.mode)") == (False, "next")
    ui.client.notify("input.key", "z")  # typed while the command sleeps: it waits its turn
    deadline = time.monotonic() + 5
    while state(ui)[0] != "paused":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert state(ui) == ("paused", "sleeper", 18)
    ui.keys("M-:")
    ui.type("got")
    ui.keys("<return>")
    assert ui.message == "'slept'"
    assert value(ui, "(_t.text[:1], _s.text.count('z'))") == ("a", 0)  # and not in the debugger
    ui.keys("<f5>")
    assert value(ui, "log") == [(1, "slept", 2)]
    assert value(ui, "_t.text[:2]") == "za"  # after the command, where it was typed


def test_step_into_an_awaited_coroutine_and_back(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'x = await inner(2)')")
    ui.eval("ed.start_task(ed.call('outer_cmd'))")
    ui.idle()
    assert state(ui) == ("paused", "outer_cmd", 23)
    ui.keys("<f11>")
    assert state(ui) == ("paused", "inner", 27)
    ui.keys("<f10>")  # over `await asyncio.sleep(0)`
    assert state(ui) == ("paused", "inner", 28)
    ui.keys("<f10>", "<f10>")  # the return, then back in the awaiting coroutine
    assert state(ui) == ("paused", "outer_cmd", 24)
    ui.keys("<f5>")
    assert value(ui, "log") == [20]


# -- breakpoints ----------------------------------------------------------------------


def test_breakpoints_follow_edits_and_survive_reevaluation(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    assert value(ui, "ed.debugger.breaks") == {"<neomacs:*scratch*>": {11: None}}
    # Two lines go in above it.  The span moves; the running code has not changed yet.
    ui.eval("_s.insert(0, '# one\\n# two\\n')")
    assert value(ui, "_bp.lines_in(_s)") == {13: None}
    ui.eval("_w.show(_s)")
    ui.call("eval_buffer")  # new code objects: the breakpoint is put on them
    ui.eval("_w.show(_t)")
    assert value(ui, "ed.debugger.breaks") == {"<neomacs:*scratch*>": {13: None}}
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    assert state(ui) == ("paused", "stepper", 13)
    ui.keys("<f5>")
    assert value(ui, "log") == [(1, 4, 5)]


def test_a_breakpoint_on_module_level_code_and_one_set_before_the_code_exists(
    ui: HeadlessUI,
) -> None:
    ui.eval(
        "_s.replace(0, len(_s), 'first = 1\\nsecond = first + 1\\ndef later():\\n    return second\\n'); _w.show(_s)"
    )
    ui.eval("_w.selections.set([(_s.line_start(2),) * 2])")
    ui.keys("<f9>")
    assert ui.message == "Breakpoint at line 2; it takes effect when this code is evaluated"
    ui.client.notify("engine.call", "eval_buffer")
    ui.idle()
    assert state(ui) == ("paused", "<module>", 2)
    ui.keys("M-:")
    ui.type("first = 41")
    ui.keys("<return>", "<f5>")
    assert value(ui, "second") == 42


def test_a_breakpoint_on_a_line_without_code_stops_at_the_next_one(ui: HeadlessUI) -> None:
    define(ui, "@command\ndef spaced() -> None:\n    one = 1\n\n    # a comment\n    two = 2\n")
    ui.eval("_w.show(_s); _w.selections.set([(_s.line_start(4),) * 2])")
    ui.keys("<f9>")
    assert ui.message == "Breakpoint at line 4; it will stop at line 6, where the statement starts"
    ui.eval("_w.show(_t); ed.start_task(ed.call('spaced'))")
    ui.idle()
    assert state(ui) == ("paused", "spaced", 6)


def test_a_conditional_breakpoint(ui: HeadlessUI) -> None:
    define(
        ui,
        "seen = []\n@command\ndef loop() -> None:\n    for i in range(6):\n        seen.append(i)\n",
    )
    ui.eval("_w.show(_s); _w.selections.set([(_s.line_start(5),) * 2])")
    ui.keys("C-u", "<f9>")
    assert ui.prompt == "Stop when: "
    ui.type("i == 4")
    ui.keys("<return>")
    ui.eval("_w.show(_t); ed.start_task(ed.call('loop'))")
    ui.idle()
    assert state(ui) == ("paused", "loop", 5) and value(ui, "seen") == [0, 1, 2, 3]
    ui.keys("<f5>")
    assert state(ui)[0] == "" and value(ui, "seen") == [0, 1, 2, 3, 4, 5]


def test_the_breakpoint_builtin(ui: HeadlessUI) -> None:
    define(
        ui,
        "@command\ndef hooked() -> None:\n    secret = 7\n    breakpoint()\n    ed.message(str(secret))\n",
    )
    ui.eval("ed.start_task(ed.call('hooked'))")
    ui.idle()
    assert state(ui) == ("paused", "hooked", 4)
    assert "breakpoint()" in ui.message
    ui.keys("M-:")
    ui.type("secret = 8")
    ui.keys("<return>", "<f5>")
    assert ui.message == "8"


def test_a_pause_inside_code_run_by_a_request(ui: HeadlessUI) -> None:
    """`engine.eval` runs the code inside the handling of its own message."""
    ui.client.notify("engine.eval", "_inline = 1\nbreakpoint()\n_inline = 2")
    ui.idle()
    assert state(ui)[0] == "paused" and value(ui, "_inline") == 1
    ui.keys("<f5>")
    assert value(ui, "_inline") == 2


# -- abort, cancel, exit ----------------------------------------------------------------


def test_abort_unwinds_the_paused_command(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    assert state(ui)[0] == "paused"
    ui.keys("C-c", "a")
    assert state(ui)[0] == "" and value(ui, "log") == []
    assert ui.message == "Quit"
    ui.type("ok")  # and the editor carries on
    assert value(ui, "_t.text[:2]") == "ok"


def test_quit_while_paused_cancels_the_debuggers_command_not_the_paused_one(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    ui.keys("M-x")
    assert ui.prompt == "M-x "
    ui.keys("C-g")
    assert ui.prompt is None and state(ui) == ("paused", "stepper", 11)
    ui.keys("M-x")
    ui.client.notify("input.interrupt")
    ui.keys("C-g")
    ui.idle()
    assert ui.prompt is None and state(ui) == ("paused", "stepper", 11)
    ui.keys("<f5>")
    assert value(ui, "log") == [(1, 4, 5)]


def test_a_prompt_left_open_is_dismissed_when_the_command_resumes(ui: HeadlessUI) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    ui.keys("M-x")
    ui.client.notify("engine.call", "debug.continue")
    ui.idle()
    assert ui.prompt is None and state(ui)[0] == "" and value(ui, "log") == [(1, 4, 5)]


def test_stepping_commands_need_a_pause(ui: HeadlessUI) -> None:
    ui.keys("M-x")
    ui.type("debug.next")
    ui.keys("<return>")
    assert ui.message == "Nothing is paused"


# -- after an error -------------------------------------------------------------------


def test_inspecting_the_frames_of_an_error(ui: HeadlessUI) -> None:
    define(
        ui,
        "def deep(d):\n    local = d * 2\n    return {}[local]\n\n@command\ndef fails() -> None:\n    top = 5\n    deep(top)\n",
    )
    ui.call("fails")
    assert "KeyError" in ui.message
    ui.call("debug.inspect")
    assert state(ui) == ("inspecting", "deep", 3)
    assert "[inspecting]" in ui.modeline
    assert value(ui, "(_w.buffer.name, _w.buffer.line_of(_w.cursor.pos))") == ("*scratch*", 3)
    ui.keys("M-:")
    ui.type("local")
    ui.keys("<return>")
    assert ui.message == "10"
    ui.keys("C-c", "u")
    assert state(ui) == ("inspecting", "fails", 8)
    ui.keys("M-:")
    ui.type("top")
    ui.keys("<return>")
    assert ui.message == "5"
    ui.keys("<f10>")
    assert ui.message == "This code has finished running; it can be looked at, not stepped"
    ui.keys("<f5>")
    assert state(ui)[0] == "" and "[inspecting]" not in ui.modeline


def test_debug_on_raise_pauses_where_it_is_raised(ui: HeadlessUI) -> None:
    define(
        ui,
        "out = []\ndef risky(v):\n    try:\n        return int(v)\n    except ValueError:\n        return -1\n\n"
        "@command\ndef tries() -> None:\n    out.append(risky('x'))\n",
    )
    ui.eval("ed.options.debug_on_raise = 'ValueError'")
    ui.eval("ed.start_task(ed.call('tries'))")
    ui.idle()
    assert state(ui) == ("paused", "risky", 4)
    assert "ValueError raised" in ui.message
    ui.keys("M-:")
    ui.type("v")
    ui.keys("<return>")
    assert ui.message == "'x'"
    ui.keys("<f5>")
    assert state(ui)[0] == "" and value(ui, "out") == [-1]
    ui.eval("ed.options.debug_on_raise = ''; ed.start_task(ed.call('tries'))")
    ui.idle()
    assert state(ui)[0] == "" and value(ui, "out") == [-1, -1]


# -- several frames, and leaving ---------------------------------------------------------


def test_every_frame_shows_the_pause_and_any_can_end_it(ui: HeadlessUI, engine: Path) -> None:
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    with HeadlessUI(engine, rows=8, cols=50) as second:
        ui.eval("ed.start_task(ed.call('stepper'))")
        ui.idle()
        second.idle()
        assert "[paused]" in ui.modeline and "[paused]" in second.modeline
        second.eval(f"_o = ed.buffers.new('other'); ed.frames[{second.frame_id}].window.show(_o)")
        second.type("typed while paused")  # the other frame is live
        assert value(ui, "_o.text") == "typed while paused"
        second.call("debug.continue")
        second.idle()
        ui.idle()
        assert "[paused]" not in ui.modeline and "[paused]" not in second.modeline
        assert value(ui, "log") == [(1, 4, 5)]
        second.client.notify("ui.detach")


def test_an_engine_that_is_paused_still_exits_when_told() -> None:
    with running_engine() as path, Client(path) as c:
        c.notify("engine.eval", "x = 1\nbreakpoint()\nx = 2")
        deadline = time.monotonic() + 5
        while c.eval("ed.debugger.paused") != "True":
            assert time.monotonic() < deadline
        started = time.monotonic()
    assert time.monotonic() - started < 3.0  # terminated by SIGTERM, not killed after a wait

    tmp = short_tempdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "neomacs", "--listen", str(tmp / "e.sock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().startswith("NEOMACS-LISTENING")
    with Client(tmp / "e.sock") as c:
        c.notify("engine.eval", "breakpoint()")
        while c.eval("ed.debugger.paused") != "True":
            pass
        c.notify("engine.exit")
        assert proc.wait(timeout=5) == 0
    proc.stdout.close()


def test_a_prompt_that_was_open_before_the_pause_can_be_answered_during_it(ui: HeadlessUI) -> None:
    """Its answer is a future of the main loop, completed from the debug loop."""
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    ui.eval(
        "answers = []\n"
        "async def _ask():\n"
        "    answers.append(await ed.minibuffer.read('Early? '))\n"
        "ed.start_task(_ask())"
    )
    assert ui.prompt == "Early? "
    ui.client.notify("engine.call", "stepper")
    ui.idle()
    assert state(ui) == ("paused", "stepper", 11) and ui.prompt == "Early? "
    # The pause took the focus for the source; give it back to the prompt.
    ui.eval("_fr = _w.frame; _fr.focus_view(_fr.prompts[-1].view)")
    ui.type("yes")
    ui.keys("<return>")
    assert ui.prompt is None and value(ui, "answers") == []  # its task is on the frozen loop
    ui.keys("<f5>")
    assert value(ui, "(answers, log)") == (["yes"], [(1, 4, 5)])


def test_ownership_is_checked_and_changes_hands(ui: HeadlessUI) -> None:
    probe = "(__import__('threading').get_ident(), __import__('neomacs._owner')._owner.owner(), __import__('neomacs._owner')._owner.checking)"
    define(ui, STEPS)
    ui.eval("_break(_s, 'c = b + 1')")
    main = value(ui, probe)
    assert main[0] == main[1] and main[2] is True
    ui.eval("ed.start_task(ed.call('stepper'))")
    ui.idle()
    during = value(ui, probe)
    assert during[0] == during[1] != main[0]
    ui.eval("_w.show(_t)")
    ui.type("x")  # an edit from the debug loop passes the owner check
    assert value(ui, "_t.text[:1]") == "x"
    ui.keys("<f5>")
    assert value(ui, probe) == main
