"""Step 11: timers, processes and worker threads (ENGINE 11)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import Client, RemoteError
from neomacs.testing.headless import HeadlessUI

PY = sys.executable


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.01)


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=12, cols=60) as session:
        session.eval(
            f"_w = ed.frames[{session.frame_id}].window; _w.show(ed.buffers.new('work')); "
            "import threading, time; _seen = []"
        )
        yield session
        session.client.notify("ui.detach")


# -- timers -----------------------------------------------------------------------


def test_after_calls_once_on_the_owner_thread(client: Client) -> None:
    client.eval(
        "import threading, time; _seen = []; _t0 = time.monotonic()\n"
        "_h = ed.after(0.05, lambda: _seen.append((threading.get_ident(), time.monotonic() - _t0)))"
    )
    wait_for(lambda: value(client, "len(_seen)") == 1)
    ident, elapsed = value(client, "_seen[0]")
    assert ident == value(client, "ed.main_thread") and 0.04 < elapsed < 2
    time.sleep(0.15)
    assert value(client, "(len(_seen), _h.active, len(ed.timers))") == (1, False, 0)


def test_a_cancelled_timer_does_not_run(client: Client) -> None:
    client.eval("_seen = []; _h = ed.after(0.05, lambda: _seen.append(1)); _h.cancel()")
    time.sleep(0.2)
    assert value(client, "(_seen, _h.active)") == ([], False)


def test_every_repeats_until_cancelled(client: Client) -> None:
    client.eval("_seen = []\ndef _tick(): _seen.append(1)\n_h = ed.every(0.02, _tick)")
    wait_for(lambda: value(client, "len(_seen)") >= 3)
    client.eval("_h.cancel()")
    count = value(client, "len(_seen)")
    time.sleep(0.15)
    assert value(client, "len(_seen)") == count


def test_setting_a_repeating_timer_again_replaces_it(client: Client) -> None:
    client.eval("_seen = []\ndef _tick(): _seen.append(1)\n_a = ed.every(30, _tick)")
    before = value(client, "len(ed.timers)")
    client.eval("_b = ed.every(30, _tick)")
    assert value(client, "(len(ed.timers), _a.active, _b.active)") == (before, False, True)
    client.eval("_b.cancel()")


def test_a_timer_that_raises_is_reported_and_the_rest_carry_on(client: Client) -> None:
    client.eval(
        "_seen = []\ndef _bad(): raise ValueError('timer went wrong')\n"
        "ed.after(0.01, _bad); ed.after(0.05, lambda: _seen.append('later'))"
    )
    wait_for(lambda: value(client, "_seen") == ["later"])
    assert value(client, "any('timer went wrong' in m and '_bad' in m for m in ed.messages)")


def test_a_timer_may_be_a_coroutine_function(client: Client) -> None:
    client.eval(
        "_seen = []\nasync def _slow():\n    await asyncio.sleep(0.01)\n    _seen.append('done')\n"
        "ed.after(0.01, _slow)"
    )
    wait_for(lambda: value(client, "_seen") == ["done"])


def test_bad_timer_arguments_are_refused(client: Client) -> None:
    with pytest.raises(RemoteError, match="function"):
        client.eval("ed.after(1, 'not a function')")
    with pytest.raises(RemoteError, match="seconds"):
        client.eval("ed.every(0, lambda: None)")


def test_when_idle_waits_for_a_pause_and_runs_once_for_each(ui: HeadlessUI) -> None:
    ui.eval("_h = ed.when_idle(0.25, lambda: _seen.append(time.monotonic()))")
    # Keys keep arriving: no pause is long enough.
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        ui.keys("x")
        time.sleep(0.03)
    assert value(ui.client, "len(_seen)") == 0
    wait_for(lambda: value(ui.client, "len(_seen)") == 1)
    time.sleep(0.5)
    assert value(ui.client, "len(_seen)") == 1  # once for the pause, not again and again
    ui.keys("x")
    wait_for(lambda: value(ui.client, "len(_seen)") == 2)
    ui.eval("_h.cancel()")


def test_a_timer_set_by_a_command_keeps_its_frame(ui: HeadlessUI) -> None:
    ui.eval(
        "@command\n"
        "def _later():\n"
        "    ed.after(0.02, lambda: _seen.append((ed.frame.id, ed.buffer.name)))\n"
    )
    ui.call("_later")
    wait_for(lambda: value(ui.client, "len(_seen)") == 1)
    frame, buffer = value(ui.client, "_seen[0]")
    assert frame == ui.frame_id and buffer == value(ui.client, "_w.buffer.name")


def test_a_timers_owner_is_recorded_and_it_goes_with_the_owner(client: Client) -> None:
    client.eval("def _tick(): pass\n_h = ed.every(30, _tick)")
    owned = "__import__('neomacs.ownership').ownership.ownership"
    assert value(client, f"[k for k, _ in {owned}.owned('neomacs_scratch') if k == 'timer']") == [
        "timer"
    ]
    client.eval(f"{owned}.release('neomacs_scratch')")
    assert value(client, "_h.active") is False


# -- processes ---------------------------------------------------------------------


def test_run_returns_status_and_output(client: Client) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    client.eval(f"_r = await ed.run([{PY!r}, '-c', {code!r}])")
    assert value(client, "(_r.returncode, _r.stdout, _r.stderr)") == (3, "out\n", "err\n")


def test_run_gives_input_and_environment(client: Client, tmp_path: Path) -> None:
    code = "import os, sys; print(sys.stdin.read().upper() + os.environ['EXTRA'] + os.getcwd())"
    client.eval(
        f"_r = await ed.run([{PY!r}, '-c', {code!r}], input='héllo ', env={{'EXTRA': 'x '}}, "
        f"cwd={str(tmp_path)!r})"
    )
    assert value(client, "_r.stdout").strip() == f"HÉLLO x {tmp_path.resolve()}"
    assert value(client, "'PATH' in __import__('os').environ")  # added to, not replaced


def test_run_check_and_timeout(client: Client) -> None:
    with pytest.raises(RemoteError, match="exited with status 2: boom"):
        client.eval(
            f"await ed.run([{PY!r}, '-c', 'import sys; sys.exit(\"boom\") if 0 else "
            f'(print("boom", file=sys.stderr), sys.exit(2))\'], check=True)'
        )
    began = time.monotonic()
    with pytest.raises(RemoteError, match="did not finish in 0.2 seconds"):
        client.eval(f"await ed.run([{PY!r}, '-c', 'import time; time.sleep(30)'], timeout=0.2)")
    assert time.monotonic() - began < 10


def test_run_wants_a_list_and_a_program_that_exists(client: Client) -> None:
    with pytest.raises(RemoteError, match="list of arguments"):
        client.eval("await ed.run('ls -l')")
    with pytest.raises(RemoteError, match="FileNotFoundError|No such file"):
        client.eval("await ed.run(['/no/such/program'])")


def test_the_engine_serves_others_while_a_program_runs(client: Client, engine_path: Path) -> None:
    client.eval(
        f"_t = ed.start_task(ed.run([{PY!r}, '-c', 'import time; time.sleep(0.5)']), name='nap')"
    )
    with Client(engine_path) as other:
        began = time.monotonic()
        assert other.eval("1 + 1") == "2"
        assert time.monotonic() - began < 0.4
    wait_for(lambda: value(client, "_t.task.done()"))


def test_spawn_streams_output_into_a_buffer_as_it_is_written(ui: HeadlessUI) -> None:
    # The program writes a line, then waits to be told to go on: what it has
    # written must be on show before it has finished.
    code = (
        "import sys\n"
        "print('first', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('second', flush=True)\n"
        "sys.stderr.write('to stderr\\n')\n"
    )
    ui.eval(
        "_out = ed.buffers.new('output'); _w.show(_out)\n"
        f"_p = ed.spawn([{PY!r}, '-c', {code!r}], buffer=_out, input=True, "
        "on_output=_seen.append, on_exit=lambda p: _seen.append(('exit', p.returncode)))"
    )
    wait_for(lambda: value(ui.client, "_out.text") == "first\n")
    ui.idle()
    assert ui.text_rows_shown() == [" 1 first", " 2"]
    assert value(ui.client, "(_p.running, _p.returncode, _p.pid > 0, _out.modified)") == (
        True, None, True, False,
    )  # fmt: skip
    assert value(ui.client, "any(t.name.startswith('process ') for t in ed.tasks)")
    ui.eval("_p.write('go\\n')")
    wait_for(lambda: value(ui.client, "_p.running") is False)
    assert value(ui.client, "_out.text") == "first\nsecond\nto stderr\n"
    assert value(ui.client, "_seen") == [
        "first\n",
        "second\n",
        "to stderr\n",
        ("exit", 0),
    ] or value(ui.client, "(''.join(s for s in _seen if isinstance(s, str)), _seen[-1])") == (
        "first\nsecond\nto stderr\n",
        ("exit", 0),
    )
    assert value(ui.client, "(_p in ed.processes, _w.cursor.pos == len(_out))") == (False, True)


def test_a_view_that_is_not_at_the_end_is_left_where_it_is(ui: HeadlessUI) -> None:
    ui.eval(
        "_out = ed.buffers.new('output'); _out.insert(0, 'above\\n'); _w.show(_out); "
        "_w.selections.set([(0, 0)])\n"
        f"_p = ed.spawn([{PY!r}, '-c', 'print(1)'], buffer=_out)"
    )
    wait_for(lambda: value(ui.client, "_p.running") is False)
    assert value(ui.client, "(_out.text, _w.cursor.pos)") == ("above\n1\n", 0)


def test_spawn_can_be_killed_and_says_how_it_ended(client: Client) -> None:
    client.eval(
        f"_seen = []; _p = ed.spawn([{PY!r}, '-c', 'import time; time.sleep(60)'], "
        "on_exit=lambda p: _seen.append(p.returncode))"
    )
    wait_for(lambda: value(client, "_p.pid") is not None)
    client.eval("_p.kill()")
    wait_for(lambda: value(client, "_seen") != [])
    assert value(client, "(_seen[0] < 0, _p.running, await _p.wait() == _seen[0])") == (
        True, False, True,
    )  # fmt: skip


def test_killing_a_spawned_shell_takes_its_children_with_it(client: Client, tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("process groups")
    marker = tmp_path / "pid"
    line = f'{PY} -c \'import os,time; open("{marker}","w").write(str(os.getpid())); time.sleep(60)\' | cat'
    client.eval(f"_p = ed.spawn(['/bin/sh', '-c', {line!r}])")
    wait_for(lambda: marker.exists() and marker.read_text() != "")
    child = int(marker.read_text())
    client.eval("_p.kill()")

    def gone() -> bool:
        try:
            import os

            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(gone)


def test_a_program_that_cannot_be_started_is_reported_through_the_handle(client: Client) -> None:
    client.eval(
        "_seen = []; _out = ed.buffers.new('output')\n"
        "_p = ed.spawn(['/no/such/program'], buffer=_out, on_exit=lambda p: _seen.append(p.error))"
    )
    wait_for(lambda: value(client, "_p.running") is False)
    assert value(client, "(type(_p.error).__name__, _p.returncode, len(_seen))") == (
        "FileNotFoundError", 127, 1,
    )  # fmt: skip
    assert value(client, "_out.text").startswith("Could not run /no/such/program")


def test_invalid_bytes_and_split_characters_in_output(client: Client) -> None:
    # A character split across two writes arrives whole; bytes that are not
    # UTF-8 become replacement characters instead of an error.
    code = (
        "import sys, time\n"
        "w = sys.stdout.buffer\n"
        "w.write(b'\\xe6\\x97'); w.flush(); time.sleep(0.1)\n"
        "w.write(b'\\xa5 \\xff end'); w.flush()\n"
    )
    client.eval(
        f"_out = ed.buffers.new('output'); _p = ed.spawn([{PY!r}, '-c', {code!r}], buffer=_out)"
    )
    wait_for(lambda: value(client, "_p.running") is False)
    assert value(client, "_out.text") == "日 � end"


def test_old_output_is_dropped_past_the_limit(client: Client) -> None:
    client.eval(
        "ed.options.process_output_limit = 2000; _out = ed.buffers.new('output')\n"
        f"_p = ed.spawn([{PY!r}, '-c', 'for i in range(2000): print(\"line\", i)'], buffer=_out)"
    )
    wait_for(lambda: value(client, "_p.running") is False)
    text = value(client, "_out.text")
    client.eval("ed.options.unset('process_output_limit')")
    assert len(text) <= 2500 and text.endswith("line 1999\n") and text.startswith("line ")


def test_one_line_longer_than_the_limit_is_still_cut(client: Client) -> None:
    client.eval(
        "ed.options.process_output_limit = 2000; _out = ed.buffers.new('output')\n"
        f"_p = ed.spawn([{PY!r}, '-c', 'import sys; sys.stdout.write(\"x\" * 50000 + \"END\")'], "
        "buffer=_out)"
    )
    wait_for(lambda: value(client, "_p.running") is False)
    size, tail = value(client, "(len(_out), _out.text[-3:])")
    client.eval("ed.options.unset('process_output_limit')")
    assert size <= 2500 and tail == "END"


def test_a_program_the_shell_left_in_the_background_can_still_be_stopped(
    client: Client, tmp_path: Path
) -> None:
    if sys.platform == "win32":
        pytest.skip("process groups")
    # The shell exits at once.  What it started holds the output open, so the
    # handle is still being served, and stopping it must reach that program.
    marker = tmp_path / "pid"
    code = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    line = f"{PY} -c {code!r} & echo started"
    client.eval(f"_seen = []; _p = ed.spawn(['/bin/sh', '-c', {line!r}], on_output=_seen.append)")
    wait_for(lambda: value(client, "_seen") == ["started\n"] and marker.exists())
    wait_for(lambda: marker.read_text() != "")
    child = int(marker.read_text())
    time.sleep(0.3)  # the shell has gone by now
    assert value(client, "_p.running") is True
    began = time.monotonic()
    client.eval("_p.kill()")
    wait_for(lambda: value(client, "_p.running") is False)
    assert time.monotonic() - began < 5

    def gone() -> bool:
        import os

        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(gone)


def test_a_running_program_is_killed_when_the_engine_stops(tmp_path: Path) -> None:
    import os

    from neomacs.testing import running_engine

    marker = tmp_path / "pid"
    code = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    with running_engine() as path, Client(path) as c:
        c.eval(f"_p = ed.spawn([{PY!r}, '-c', {code!r}])")
        wait_for(lambda: marker.exists() and marker.read_text() != "")
        child = int(marker.read_text())

    def gone() -> bool:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(gone)


# -- worker threads -----------------------------------------------------------------


def test_in_thread_runs_elsewhere_and_returns_the_result(client: Client) -> None:
    client.eval(
        "import threading\n"
        "def _work(text): return (threading.get_ident(), text.upper())\n"
        "_r = await ed.in_thread(_work, 'snapshot')"
    )
    ident, text = value(client, "_r")
    assert text == "SNAPSHOT" and ident != value(client, "ed.main_thread")
    with pytest.raises(RemoteError, match="ZeroDivisionError"):
        client.eval("await ed.in_thread(lambda: 1 / 0)")


def test_a_worker_thread_may_not_touch_a_buffer(client: Client) -> None:
    client.eval("_b = ed.buffers.new('guarded')")
    with pytest.raises(RemoteError, match="does not own it"):
        client.eval("await ed.in_thread(_b.insert, 0, 'x')")
    assert value(client, "_b.text") == ""


# -- the shell command ----------------------------------------------------------------


def test_shell_command_streams_into_its_buffer(ui: HeadlessUI, tmp_path: Path) -> None:
    (tmp_path / "here.txt").write_text("x")
    ui.open(tmp_path / "here.txt")
    ui.keys("M-!")
    assert ui.prompt == "Shell command: "
    gate = tmp_path / "gate"
    ui.type(f"echo started; pwd; while [ ! -e {gate} ]; do sleep 0.05; done; echo finished; exit 4")
    ui.keys("<return>")
    out = "ed.buffers.get('*shell output*')"
    wait_for(lambda: "started" in value(ui.client, f"{out}.text"))
    # Still running: the editor takes keys, and a second command is refused.
    ui.keys("C-e", "y")
    assert value(ui.client, "ed.buffers.get('here.txt').text") == "xy"
    assert value(ui.client, f"{out}.locals['process'].running") is True
    ui.keys("M-!")
    ui.type("echo other")
    ui.keys("<return>")
    assert "still running" in ui.message
    gate.write_text("")
    wait_for(lambda: value(ui.client, f"{out}.locals['process'].running") is False)
    text = value(ui.client, f"{out}.text")
    assert text.startswith("$ echo started; pwd;") and text.endswith("finished\n")
    assert str(tmp_path.resolve()) in text  # it ran where the file is
    ui.idle()
    assert ui.message.startswith("Exited with status 4")
    assert value(ui.client, f"({out}.read_only, {out}.modified)") == (True, False)


def test_a_shell_command_can_be_interrupted_from_its_buffer(ui: HeadlessUI) -> None:
    ui.call("shell_command", "sleep 60")
    out = "ed.buffers.get('*shell output*')"
    wait_for(lambda: value(ui.client, f"{out}.locals['process'].pid") is not None)
    ui.eval(f"ed.show({out}, focus=True)")
    ui.keys("C-c", "C-c")
    wait_for(lambda: value(ui.client, f"{out}.locals['process'].running") is False)
    ui.idle()
    assert ui.message.startswith("Stopped by signal 2")
    ui.keys("C-c", "C-k")
    assert ui.message == "No program is running here"
