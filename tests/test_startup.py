"""Step 11: directories, the init file, single-file extensions, `--safe` (EXTENSIONS X0)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from neomacs import paths
from neomacs.testing import Client, running_engine
from neomacs.testing.headless import HeadlessUI


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


def env_for(config: Path) -> dict[str, str]:
    return {"NEOMACS_CONFIG_DIR": str(config)}


# -- where things live ------------------------------------------------------------


def test_directories_follow_the_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("NEOMACS_CONFIG_DIR", "NEOMACS_DATA_DIR", "NEOMACS_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xd"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xk"))
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.config_dir() == tmp_path / "xc" / "neomacs"
    assert paths.data_dir() == tmp_path / "xd" / "neomacs"
    assert paths.cache_dir() == tmp_path / "xk" / "neomacs"
    monkeypatch.setattr(sys, "platform", "darwin")
    # Configuration stays under ~/.config on macOS: it is meant to be edited.
    assert paths.config_dir() == tmp_path / "xc" / "neomacs"
    assert paths.data_dir() == Path.home() / "Library" / "Application Support" / "neomacs"
    assert paths.cache_dir() == Path.home() / "Library" / "Caches" / "neomacs"
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var)
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.config_dir() == Path.home() / ".config" / "neomacs"
    assert paths.data_dir() == Path.home() / ".local" / "share" / "neomacs"
    assert paths.cache_dir() == Path.home() / ".cache" / "neomacs"


def test_each_directory_can_be_put_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEOMACS_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("NEOMACS_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("NEOMACS_CACHE_DIR", str(tmp_path / "k"))
    assert paths.init_file() == tmp_path / "c" / "init.py"
    assert paths.extensions_dir() == tmp_path / "c" / "extensions"
    assert paths.journal_dir() == tmp_path / "d" / "journal"
    assert paths.cache_dir() == tmp_path / "k"


def test_a_test_engine_keeps_to_directories_of_its_own(client: Client) -> None:
    config, data = value(
        client, "tuple(str(p) for p in (__import__('neomacs.paths').paths.config_dir(), "
        "__import__('neomacs.paths').paths.data_dir()))"
    )  # fmt: skip
    assert str(Path.home() / ".config") not in config and "Library" not in data


# -- the init file -----------------------------------------------------------------

INIT = """\
from neomacs import ed
from neomacs.commands import command
from neomacs.keymap import global_map
from neomacs.options import Option

Option("greeting", str, "hello", "What to say.")
ed.options.tab_width = 3


@command
def greet() -> None:
    '''Say the greeting.'''
    ed.message(ed.options.greeting + " from init")


global_map.bind("<f7>", "greet")
loaded_in = __name__
"""


def test_configuration_survives_a_restart(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "init.py").write_text(INIT)
    for _ in range(2):  # the same in a second engine, with nothing carried over but the file
        with running_engine(env=env_for(config)) as path, HeadlessUI(path, rows=8, cols=50) as ui:
            assert value(ui.client, "ed.options.tab_width") == 3
            ui.keys("<f7>")
            assert ui.message == "hello from init"
            assert value(ui.client, "__import__('sys').modules['neomacs_init'].loaded_in") == (
                "neomacs_init"
            )


def test_the_init_file_is_loaded_last_and_owns_what_it_registers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "extensions").mkdir(parents=True)
    (config / "extensions" / "zeta.py").write_text(
        "from neomacs.keymap import global_map\n"
        "from neomacs.commands import command\n"
        "order = ['zeta']\n"
        "@command(name='zeta.hello')\n"
        "def hello() -> None: pass\n"
        "global_map.bind('<f8>', 'zeta.hello')\n"
    )
    (config / "extensions" / "alpha.py").write_text(
        "import sys\nseen_zeta = 'neomacs_user.zeta' in sys.modules\n"
    )
    (config / "extensions" / "_private.py").write_text("raise RuntimeError('not loaded')\n")
    (config / "extensions" / "not-a-name.py").write_text("raise RuntimeError('not loaded')\n")
    (config / "init.py").write_text(
        INIT + "import neomacs_user.zeta as z\nz.order.append('init')\n"
        "global_map.bind('<f8>', 'greet')\n"
    )
    with running_engine(env=env_for(config)) as path, HeadlessUI(path, rows=8, cols=50) as ui:
        c = ui.client
        assert value(c, "__import__('sys').modules['neomacs_user.zeta'].order") == ["zeta", "init"]
        # In name order: alpha was loaded before zeta existed.
        assert value(c, "__import__('sys').modules['neomacs_user.alpha'].seen_zeta") is False
        assert (
            value(c, "[m for m in __import__('sys').modules if 'private' in m or 'not-a' in m]")
            == []
        )
        ui.keys("<f8>")  # the init file's binding, not the extension's
        assert ui.message == "hello from init"
        own = "__import__('neomacs.ownership').ownership.ownership"
        assert value(c, f"{own}.owner_of('command', 'greet')") == "neomacs_init"
        assert value(c, f"{own}.owner_of('command', 'zeta.hello')") == "neomacs_user.zeta"
        assert value(c, f"{own}.owner_of('option', 'greeting')") == "neomacs_init"
        assert value(c, "[m for m in ed.messages if 'failed' in m or 'Error' in m]") == []


def test_evaluating_the_init_file_again_replaces_what_it_registered(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "init.py").write_text(INIT)
    with running_engine(env=env_for(config)) as path, HeadlessUI(path, rows=8, cols=50) as ui:
        ui.call("init.open")
        assert ui.view.info["name"] == "init.py"
        # Rename the command: the old name must go, as it would after a restart.
        ui.eval(
            "_b = ed.buffer; _t = _b.text.replace('def greet', 'def salute')"
            ".replace('\"greet\"', '\"salute\"'); _b.replace(0, len(_b), _t)"
        )
        ui.call("eval_buffer")
        assert value(ui.client, "sorted(n for n in ed.commands if n in ('greet', 'salute'))") == [
            "salute"
        ]
        ui.keys("C-x", "b")
        ui.type("*scratch*")
        ui.keys("<return>", "<f7>")
        assert ui.message == "hello from init"


def test_a_broken_init_file_is_reported_and_the_engine_still_starts(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "extensions").mkdir(parents=True)
    (config / "extensions" / "bad.py").write_text("def broken(:\n")
    (config / "extensions" / "good.py").write_text("ok = True\n")
    (config / "init.py").write_text("before = 1\nraise RuntimeError('init went wrong')\n")
    with running_engine(env=env_for(config)) as path, HeadlessUI(path, rows=8, cols=70) as ui:
        c = ui.client
        # The first frame is told, since nobody was looking when it happened.
        assert ui.message.startswith("extension bad failed to load: SyntaxError")
        assert "(and 1 more)" in ui.message
        messages = value(c, "ed.messages")
        assert any("init.py" in m and "init went wrong" in m for m in messages)
        assert value(c, "__import__('sys').modules['neomacs_user.good'].ok") is True
        assert value(c, "__import__('sys').modules['neomacs_init'].before") == 1
        # And the traceback of the last failure can be looked at.
        assert "init went wrong" in value(c, "ed.buffers.get('*traceback*').text")
        ui.keys("x")
        assert value(c, "ed.buffer.text").endswith("x")
    with running_engine(env=env_for(config)) as path, HeadlessUI(path, rows=8, cols=70) as ui:
        with HeadlessUI(path, rows=8, cols=70) as second:
            assert second.message == ""  # said once, to the first frame


def test_safe_loads_neither(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "extensions").mkdir(parents=True)
    (config / "extensions" / "ext.py").write_text("loaded = True\n")
    (config / "init.py").write_text(INIT)
    with running_engine(env=env_for(config), args=["--safe"]) as path, Client(path) as c:
        mods = value(c, "[m for m in __import__('sys').modules if m.startswith('neomacs_')]")
        assert "neomacs_init" not in mods and "neomacs_user.ext" not in mods
        assert value(c, "(ed.safe, ed.options.tab_width, 'greet' in ed.commands)") == (
            True, 8, False,
        )  # fmt: skip
        assert value(c, "any('--safe' in m for m in ed.messages)")


def test_the_init_file_may_await_and_set_timers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "init.py").write_text(
        "import sys\nfrom neomacs import ed\n"
        "done = await ed.run([sys.executable, '-c', 'print(42)'])\n"
        "ticks = []\n"
        "def tick() -> None: ticks.append(1)\n"
        "ed.every(0.02, tick)\n"
    )
    with running_engine(env=env_for(config)) as path, Client(path) as c:
        mod = "__import__('sys').modules['neomacs_init']"
        assert value(c, f"{mod}.done.stdout") == "42\n"
        import time

        deadline = time.monotonic() + 5
        while value(c, f"len({mod}.ticks)") < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_no_configuration_at_all_is_not_an_error(tmp_path: Path) -> None:
    with running_engine(env=env_for(tmp_path / "absent")) as path, Client(path) as c:
        assert value(c, "[m for m in ed.messages if 'rror' in m]") == []
        assert not (tmp_path / "absent").exists()  # and nothing is created by starting
