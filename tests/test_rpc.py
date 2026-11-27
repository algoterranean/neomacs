from __future__ import annotations

import json
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from neomacs import paths
from neomacs.testing import Client, RemoteError, running_engine


def test_eval_expression(client: Client) -> None:
    assert client.eval("1 + 1") == "2"


def test_eval_statements_then_expression(client: Client) -> None:
    assert client.eval("x = 20\ny = 22\nx + y") == "42"
    assert client.eval("x") == "20"  # the scratch module persists


def test_eval_statement_has_no_value(client: Client) -> None:
    assert client.eval("z = 1") is None


def test_exception_is_an_error_and_engine_survives(client: Client) -> None:
    with pytest.raises(RemoteError) as info:
        client.eval("1 / 0")
    assert "ZeroDivisionError" in str(info.value)
    assert info.value.data["type"] == "ZeroDivisionError"
    assert "Traceback" in info.value.data["traceback"]
    assert client.eval("'still here'") == "'still here'"


def test_syntax_error_is_an_error(client: Client) -> None:
    with pytest.raises(RemoteError, match="SyntaxError"):
        client.eval("def (")
    assert client.eval("2") == "2"


def test_ed_is_available_in_scratch(client: Client) -> None:
    assert client.eval("type(ed).__name__") == "'Editor'"
    assert client.eval("import neomacs; neomacs.ed.loop is ed.loop") == "True"


def test_api_info(client: Client) -> None:
    info = client.call("engine.api_info")
    assert info["protocol"] == 1
    names = {m["name"] for m in info["methods"]}
    assert {"engine.api_info", "engine.eval", "engine.call", "engine.exit"} <= names
    eval_info = next(m for m in info["methods"] if m["name"] == "engine.eval")
    assert [p["name"] for p in eval_info["params"]] == ["source", "mode"]
    assert eval_info["params"][1]["optional"] is True
    assert eval_info["doc"]


def test_unknown_method(client: Client) -> None:
    with pytest.raises(RemoteError) as info:
        client.call("no.such.method")
    assert info.value.code == -32601


def test_invalid_params(client: Client) -> None:
    with pytest.raises(RemoteError) as info:
        client.call("engine.eval")
    assert info.value.code == -32602


def test_unknown_command(client: Client) -> None:
    with pytest.raises(RemoteError, match="no such command"):
        client.call("engine.call", "nope")


def test_unknown_notification_is_ignored(client: Client) -> None:
    client.notify("no.such.notification", 1, 2)
    assert client.eval("1") == "1"
    assert "unknown notification" in client.eval("ed.messages[-1]")


def test_garbage_line_gets_parse_error(engine_path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(engine_path))
    sock.sendall(b"this is not json\n")
    reply = json.loads(sock.makefile("rb").readline())
    assert reply["error"]["code"] == -32700
    sock.close()


def test_two_clients_share_one_engine(engine_path: Path) -> None:
    with Client(engine_path) as a, Client(engine_path) as b:
        a.eval("shared = 'from a'")
        assert b.eval("shared") == "'from a'"


def test_engine_can_send_requests_to_client(client: Client) -> None:
    client.request_handlers["test.double"] = lambda n: n * 2
    client.eval(
        "import asyncio\n"
        "conn = next(iter(ed.connections))\n"
        "results = []\n"
        "async def ask():\n"
        "    for c in list(ed.connections):\n"
        "        try:\n"
        "            results.append(await c.request('test.double', 21, timeout=2))\n"
        "        except Exception as e:\n"
        "            results.append(type(e).__name__)\n"
        "ed.keep_task(asyncio.get_running_loop().create_task(ask()))\n"
    )
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "42" in client.eval("results"):
            break
        time.sleep(0.02)
    assert "42" in client.eval("results")


def test_socket_is_private(engine_path: Path) -> None:
    assert engine_path.stat().st_mode & 0o077 == 0


def test_gil_stays_disabled(client: Client) -> None:
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("not a free-threaded build")
    assert client.eval("import sys; sys._is_gil_enabled()") == "False"


def test_exit_request_stops_engine() -> None:
    with running_engine() as path:
        c = Client(path)
        c.notify("engine.exit")
        assert c.closed.wait(5)


def test_socket_path_length_check() -> None:
    with pytest.raises(OSError, match="too long"):
        paths.check_socket_path(Path("/tmp") / ("x" * 120) / "e.sock")
    assert len(str(paths.socket_path("test"))) <= paths.MAX_SOCKET_PATH


def test_version_flag() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "neomacs", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert "neomacs" in out and "protocol 1" in out


def test_stdio_transport() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "neomacs", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    request = {"jsonrpc": "2.0", "id": 1, "method": "engine.eval", "params": ["6 * 7"]}
    proc.stdin.write(json.dumps(request).encode() + b"\n")
    proc.stdin.flush()
    reply = json.loads(proc.stdout.readline())
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": "42"}
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"engine.exit","params":[]}\n')
    proc.stdin.flush()
    assert proc.wait(timeout=5) == 0
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def test_stdio_reply_survives_input_closing_early() -> None:
    lines = (
        b'{"jsonrpc":"2.0","id":1,"method":"engine.eval","params":["1 + 1"]}\n'
        b'{"jsonrpc":"2.0","method":"engine.exit","params":[]}\n'
    )
    done = subprocess.run(
        [sys.executable, "-m", "neomacs", "--stdio"], input=lines, capture_output=True, timeout=10
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["result"] == "2"


def test_retrying_a_deferred_request_does_not_hold_a_lock_while_the_handler_runs(
    client: Client,
) -> None:
    """A handler takes its own lock.  One thread retries deferred requests while
    holding that lock (having just applied a batch); a timer retries them
    without it.  Each must not wait for the other for ever."""
    import threading

    from neomacs.testing import NotReady

    outer = threading.Lock()
    entered = threading.Event()

    def handler() -> str:
        entered.set()
        with outer:
            raise NotReady("still not ready")

    client.defer_seconds = 30  # no timer of its own during the test
    client.request_handlers["test.slow"] = handler
    client._deferred.append(
        ({"id": 999001, "method": "test.slow", "params": []}, time.monotonic() + 30)
    )
    with outer:
        timer = threading.Thread(target=client.retry_deferred, daemon=True)
        timer.start()
        assert entered.wait(5)  # the timer's retry is inside the handler, waiting for our lock
        finished = threading.Event()
        holder = threading.Thread(
            target=lambda: (client.retry_deferred(), finished.set()), daemon=True
        )
        holder.start()
        assert finished.wait(5), (
            "retry_deferred waited for the other retry while we hold the handler's lock"
        )
    timer.join(5)
    assert not timer.is_alive()
    client._deferred.clear()
    del client.request_handlers["test.slow"]
