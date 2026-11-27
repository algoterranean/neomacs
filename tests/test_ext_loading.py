"""Step 12, X1 and X3: extensions in a running editor: lazy loading,
disabling, reloading, and installing from inside."""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from extkit import config, dirs, extension, library, needs_uv

from neomacs.ext import manifest as manifest_module
from neomacs.ext.environment import Environment
from neomacs.testing import Client, running_engine
from neomacs.testing.headless import HeadlessUI

OWN = "__import__('neomacs.ownership').ownership.ownership"
MODS = "__import__('sys').modules"


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


def wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.02)


def install(root: Path, *names: str) -> Environment:
    """Install the named distributions from ROOT/wheels into ROOT's environment."""
    config(root / "config", root / "wheels")
    env = Environment(config=root / "config", data=root / "data", cache=root / "cache")

    async def go() -> None:
        for name in names:
            wanted = manifest_module.parse_request(name)
            await env.apply(await env.plan(env.with_change(add=wanted)), add=wanted)

    asyncio.run(go())
    return env


LAZY = """\
[autoload]
commands = [{ name = "lazy.hello", doc = "Say hello, lazily." }, "lazy.other"]
modes = [{ name = "lazy-notes", patterns = ["*.lazynotes"] }]
"""

LAZY_SOURCE = """\
from neomacs import ed
from neomacs.commands import command
from neomacs.modes import TextMode


class LazyNotes(TextMode):
    '''Notes, lazily.'''
    name = "lazy-notes"
    patterns = ["*.lazynotes"]


@command(name="lazy.hello")
def hello() -> None:
    '''Say hello, lazily, and at more length than the index does.'''
    ed.message("hello from lazy")


@command(name="lazy.other")
def other() -> None:
    ed.message("the other")
"""


# -- startup -----------------------------------------------------------------------------


@needs_uv
def test_a_lazy_extension_is_not_imported_until_it_is_used(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    extension(wheels, "lazy", source=LAZY_SOURCE, declarations=LAZY)
    extension(wheels, "eager")
    install(tmp_path, "neomacs-lazy", "neomacs-eager")
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=70) as ui:
        c = ui.client
        states = "{e.name: e.state for e in ed.extensions}"
        assert value(c, states) == {"eager": "loaded", "lazy": "known"}
        # One that declares nothing is imported; one that declares what it has is not.
        assert value(c, f"('neomacs_eager' in {MODS}, 'neomacs_lazy' in {MODS})") == (True, False)
        # Its commands are there all the same, with the line about them from the index.
        assert value(c, "ed.commands['lazy.hello'].summary") == "Say hello, lazily."
        assert value(c, f"{OWN}.owner_of('command', 'lazy.hello')") == "autoload:lazy"
        ui.keys("M-x")
        ui.type("lazy.hel")
        assert ui.candidates() == ["lazy.hello"]
        ui.keys("<return>")
        assert ui.message == "hello from lazy"
        assert value(c, f"'neomacs_lazy' in {MODS}") is True
        assert value(c, states) == {"eager": "loaded", "lazy": "loaded"}
        # The real commands have taken the stand-ins' places, both of them.
        assert value(c, "ed.commands['lazy.hello'].summary").endswith("than the index does.")
        assert value(c, f"{OWN}.owner_of('command', 'lazy.other')") == "neomacs_lazy"
        assert value(c, f"{OWN}.owned('autoload:lazy')") == []
        ui.call("lazy.other")
        assert ui.message == "the other"
        ui.call("eager.hello")
        assert ui.message == "hello from eager 0.1"


@needs_uv
def test_visiting_a_file_loads_the_extension_that_has_its_mode(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    extension(wheels, "lazy", source=LAZY_SOURCE, declarations=LAZY)
    install(tmp_path, "neomacs-lazy")
    (tmp_path / "plain.txt").write_text("x\n")
    (tmp_path / "today.lazynotes").write_text("x\n")
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=70) as ui:
        ui.open(tmp_path / "plain.txt")
        assert value(ui.client, f"'neomacs_lazy' in {MODS}") is False
        ui.open(tmp_path / "today.lazynotes")
        assert value(ui.client, "ed.buffer.mode.name") == "lazy-notes"
        assert value(ui.client, f"'neomacs_lazy' in {MODS}") is True
        assert "(lazy-notes)" in ui.modeline
    # By name, too: an init file may ask for the mode before any file is visited.
    with running_engine(env=dirs(tmp_path)) as path, Client(path) as c:
        assert value(c, "ed.buffers.new('n').set_mode('lazy-notes').name") == "lazy-notes"


@needs_uv
def test_extensions_load_in_dependency_order_before_the_users_own_code(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    record = "import builtins\nbuiltins._order = getattr(builtins, '_order', []) + [{name!r}]\n"
    # `base` sorts after `app` by name; `app` depends on it, so `base` goes first.
    extension(wheels, "zbase", source=record.format(name="zbase"))
    extension(wheels, "app", source=record.format(name="app"), requires=("neomacs-zbase",))
    install(tmp_path, "neomacs-app")
    extensions = tmp_path / "config" / "extensions"
    extensions.mkdir()
    (extensions / "mine.py").write_text(record.format(name="single file"))
    (tmp_path / "config" / "init.py").write_text(record.format(name="init"))
    with running_engine(env=dirs(tmp_path)) as path, Client(path) as c:
        assert value(c, "__import__('builtins')._order") == ["zbase", "app", "single file", "init"]
        kinds = "{e.name: e.kind for e in ed.extensions}"
        assert value(c, kinds) == {"zbase": "package", "app": "package", "mine": "file"}


@needs_uv
def test_one_that_fails_is_reported_and_the_rest_load_and_safe_loads_none(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    extension(wheels, "broken", source="raise RuntimeError('this one is broken')\n")
    extension(wheels, "fine")
    install(tmp_path, "neomacs-broken", "neomacs-fine")
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=90) as ui:
        assert ui.message.startswith("extension broken failed to load: RuntimeError: this one")
        states = value(ui.client, "{e.name: (e.state, e.error) for e in ed.extensions}")
        assert states == {
            "broken": ("failed", "RuntimeError: this one is broken"),
            "fine": ("loaded", ""),
        }
        ui.call("fine.hello")
        assert ui.message == "hello from fine 0.1"
    with running_engine(env=dirs(tmp_path), args=["--safe"]) as path, Client(path) as c:
        assert value(c, "(list(ed.extensions), 'fine.hello' in ed.commands)") == ([], False)
        assert value(c, f"[m for m in {MODS} if m.startswith('neomacs_f')]") == []


@needs_uv
def test_an_extension_cannot_shadow_the_engine(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    # It ships a package called `neomacs` and a module called `json`.
    extension(wheels, "shadow", extra={"../neomacs/__init__.py": "raise SystemExit('shadowed')\n",
                                       "../json.py": "raise SystemExit('shadowed')\n"})  # fmt: skip
    install(tmp_path, "neomacs-shadow")
    with running_engine(env=dirs(tmp_path)) as path, Client(path) as c:
        assert value(c, "ed.extensions.get('shadow').state") == "loaded"
        site = str(Environment(config=tmp_path / "c", data=tmp_path / "data").site_packages())
        assert value(c, "__import__('neomacs').__file__").startswith(site) is False
        assert value(c, "__import__('json').__file__").startswith(site) is False
        assert value(c, "__import__('sys').path[-1]") == site  # after everything of the engine's


@needs_uv
def test_global_keys_bound_on_load_are_allowed_and_said(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    extension(
        wheels,
        "keys",
        source="from neomacs.keymap import Keymap, global_map\n"
        "global_map.bind('C-c k', 'forward_char')\nglobal_map.bind('<f9>', 'forward_char')\n"
        "own = Keymap('own'); own.bind('x', 'forward_char')\n",
    )
    install(tmp_path, "neomacs-keys")
    with running_engine(env=dirs(tmp_path)) as path, Client(path) as c:
        said = [m for m in value(c, "ed.messages") if "global map" in m]
        assert said == ["The extension keys bound keys in the global map: <f9>, C-c k"]
        assert value(c, "__import__('neomacs.keymap').keymap.global_map.lookup(['<f9>'])") == (
            "forward_char"
        )


@needs_uv
def test_the_extension_that_turned_the_gil_back_on_is_recorded(tmp_path: Path) -> None:
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("not a free-threaded build")
    probe = subprocess.run(
        [sys.executable, "-c", "import sys, tree_sitter; print(sys._is_gil_enabled())"],
        capture_output=True, text=True,
    )  # fmt: skip
    if probe.stdout.strip() != "True":
        pytest.skip("no module at hand that turns the GIL back on when imported")
    wheels = tmp_path / "wheels"
    extension(wheels, "innocent")
    extension(wheels, "ztree", source="import tree_sitter\n")
    install(tmp_path, "neomacs-innocent", "neomacs-ztree")
    env = {**dirs(tmp_path), "PYTHON_GIL": ""}
    with running_engine(env=env) as path, HeadlessUI(path, rows=10, cols=100) as ui:
        c = ui.client
        assert value(c, "{e.name: e.gil for e in ed.extensions}") == {
            "innocent": False,
            "ztree": True,
        }
        assert any("ztree turned the GIL back on" in m for m in value(c, "ed.messages"))
        ui.call("extensions.list")
        listing = value(c, "ed.buffers.get('*extensions*').text")
        assert "ztree  0.1  loaded  turned the GIL on" in listing and "The GIL is on." in listing
        assert "innocent  0.1  loaded  The innocent extension" in listing


# -- disabling and reloading (X1) -----------------------------------------------------------

SINGLE = """\
from neomacs import ed
from neomacs.commands import command
from neomacs.hooks import post_command
from neomacs.keymap import global_map
from neomacs.modes import Mode, TextMode
from neomacs.options import Option

Option("single_volume", int, 3, "How loud.")
ticks = []


class SingleMode(TextMode):
    name = "single"


class SingleMinor(Mode):
    name = "single-minor"
    minor = True


@command(name="single.hello")
def hello() -> None:
    ed.message(f"volume {ed.options.single_volume}")


@post_command.add
def count(command) -> None:
    ticks.append(command.name)


def tick() -> None:
    ticks.append("tick")


async def forever() -> None:
    import asyncio
    await asyncio.sleep(3600)


global_map.bind("<f6>", "single.hello")
ed.every(3600, tick)
ed.start_task(forever(), name="single forever")
"""


def traces(c: Client, module: str) -> dict[str, Any]:
    """Everything an extension could have left in the editor."""
    return {
        "modules": value(
            c, f"sorted(m for m in {MODS} if m == {module!r} or m.startswith({module!r} + '.'))"
        ),
        "owned": value(c, f"sorted(k for k, _ in {OWN}.owned({module!r}))"),
        "commands": value(c, "sorted(n for n in ed.commands if n.startswith('single'))"),
        "options": value(
            c,
            "sorted(n for n in __import__('neomacs.options').options.options if n.startswith('single'))",
        ),
        "modes": value(
            c,
            "sorted(n for n in __import__('neomacs.modes').modes.modes if n.startswith('single'))",
        ),
        "hook": value(
            c,
            f"[f.__name__ for f in __import__('neomacs.hooks').hooks.post_command.functions() if f.__module__ == {module!r}]",
        ),
        "key": value(c, "__import__('neomacs.keymap').keymap.global_map.lookup(['<f6>'])"),
        "timers": value(c, "[t.name for t in ed.timers if t.name == 'tick']"),
        "tasks": value(c, "[t.name for t in ed.tasks if t.name == 'single forever']"),
    }


def test_a_single_file_extension_is_disabled_and_enabled_with_no_trace_between(
    tmp_path: Path,
) -> None:
    extensions = tmp_path / "config" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "single.py").write_text(SINGLE)
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=70) as ui:
        c = ui.client
        module = "neomacs_user.single"
        before = traces(c, module)
        assert before == {
            "modules": [module],
            "owned": [
                "command",
                "command",
                "command",
                "hook",
                "key",
                "mode",
                "mode",
                "option",
                "timer",
            ],
            "commands": ["single.hello", "single_minor_mode", "single_mode"],
            "options": ["single_volume"],
            "modes": ["single", "single-minor"],
            "hook": ["count"],
            "key": "single.hello",
            "timers": ["tick"],
            "tasks": ["single forever"],
        }
        # In use: a buffer in its mode with its minor mode on, and its option set.
        ui.eval(
            "_b = ed.buffers.new('in its mode'); _b.set_mode('single'); "
            "_b.enable_minor(__import__('neomacs.modes').modes.lookup('single-minor')); "
            "ed.options.single_volume = 11"
        )
        ui.keys("<f6>")
        assert ui.message == "volume 11"

        ui.keys("M-x")
        ui.type("extensions.disable")
        ui.keys("<return>")
        ui.type("single")
        ui.keys("<return>")
        assert ui.message == "Disabled single"
        assert traces(c, module) == {
            "modules": [], "owned": [], "commands": [], "options": [], "modes": [], "hook": [],
            "key": None, "timers": [], "tasks": [],
        }  # fmt: skip
        assert value(c, "(_b.mode.name, [m.name for m in _b.minor_modes])") == ("fundamental", [])
        assert value(c, "ed.extensions.get('single').state") == "disabled"
        ui.keys("<f6>")
        assert ui.message == "<f6> is undefined"

        ui.keys("M-x")
        ui.type("extensions.enable")
        ui.keys("<return>")
        ui.type("single")
        ui.keys("<return>")
        assert ui.message == "Enabled single"
        assert traces(c, module) == before
        ui.keys("<f6>")
        assert ui.message == "volume 11"  # what the user set for its option was kept


@needs_uv
def test_a_package_extension_is_disabled_reloaded_and_told_to_wait_for_a_restart(
    tmp_path: Path,
) -> None:
    wheels = tmp_path / "wheels"
    source = (
        "from neomacs import ed\nfrom neomacs.commands import command\nfrom . import helper\n"
        "said = []\ndef activate(): said.append('activated')\n"
        "def deactivate(): __import__('builtins')._deactivated = True\n"
        "@command(name='pack.hello')\ndef hello() -> None: ed.message(helper.WORD)\n"
        "@command(name='pack.old')\ndef old() -> None: pass\n"
    )
    extension(wheels, "pack", source=source, extra={"helper.py": "WORD = 'first'\n"})
    extension(wheels, "stiff", declarations="requires_restart = true\n")
    env = install(tmp_path, "neomacs-pack", "neomacs-stiff")
    site = env.site_packages()
    assert site is not None
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=90) as ui:
        c = ui.client
        assert value(c, f"{MODS}['neomacs_pack'].said") == ["activated"]
        ui.call("pack.hello")
        assert ui.message == "first"
        # Its files change on disk, as an upgrade would change them.
        (site / "neomacs_pack" / "helper.py").write_text("WORD = 'second'\n")
        (site / "neomacs_pack" / "__init__.py").write_text(source.replace("pack.old", "pack.new"))
        held = value(c, f"id({MODS}['neomacs_pack'].hello)")
        ui.eval("await ed.extensions.reload('pack')")
        ui.call("pack.hello")
        assert ui.message == "second"
        # The function was updated in place, and the command it no longer defines has gone.
        assert value(c, f"id({MODS}['neomacs_pack'].hello)") == held
        assert value(c, "sorted(n for n in ed.commands if n.startswith('pack.'))") == [
            "pack.hello", "pack.new",
        ]  # fmt: skip

        ui.eval("ed.extensions.disable('pack')")
        assert value(c, "__import__('builtins')._deactivated") is True
        assert value(c, f"[m for m in {MODS} if m.startswith('neomacs_pack')]") == []
        assert value(c, "[n for n in ed.commands if n.startswith('pack.')]") == []
        ui.eval("await ed.extensions.enable('pack')")
        ui.call("pack.hello")
        assert ui.message == "second"

        # One that says it does not survive reloading is left running as it is.
        ui.keys("M-x")
        ui.type("extensions.reload")
        ui.keys("<return>")
        ui.type("stiff")
        ui.keys("<return>")
        assert ui.message.startswith("stiff cannot be reloaded: it says it does not survive")
        assert value(c, "ed.extensions.get('stiff').state") == "restart"
        ui.call("stiff.hello")
        assert ui.message == "hello from stiff 0.1"
        ui.call("extensions.list")
        listing = value(c, "ed.buffers.get('*extensions*').text")
        assert "stiff  0.1  restart to finish" in listing
        assert "Some changes need the editor to be restarted" in listing


@needs_uv
def test_an_extension_that_fails_part_way_leaves_nothing_registered(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    source = (
        "from neomacs.commands import command\nfrom neomacs.keymap import global_map\n"
        "from . import part\n"
        "@command(name='half.made')\ndef made() -> None: pass\n"
        "global_map.bind('<f9>', 'half.made')\n"
        "raise RuntimeError('and then it failed')\n"
    )
    part = (
        "from neomacs.commands import command\n@command(name='half.part')\ndef p() -> None: pass\n"
    )
    extension(wheels, "half", source=source, extra={"part.py": part})
    install(tmp_path, "neomacs-half")
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=90) as ui:
        c = ui.client
        assert value(c, "ed.extensions.get('half').state") == "failed"
        assert value(c, "[n for n in ed.commands if n.startswith('half.')]") == []
        assert value(c, f"[o for o in {OWN}.owners() if o.startswith('neomacs_half')]") == []
        assert value(c, f"[m for m in {MODS} if m.startswith('neomacs_half')]") == []
        ui.keys("<f9>")
        assert ui.message == "<f9> is undefined"


@needs_uv
def test_an_extension_with_a_compiled_module_is_not_half_reloaded(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    source = (
        "from neomacs import ed\nfrom neomacs.commands import command\n"
        "@command(name='mixed.hello')\ndef hello() -> None: ed.message('old source')\n"
    )
    extension(wheels, "mixed", source=source)
    env = install(tmp_path, "neomacs-mixed")
    site = env.site_packages()
    assert site is not None
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=90) as ui:
        # A compiled module of its own, imported after the package was: later in the order.
        ui.eval(
            "import sys, types\n_fast = types.ModuleType('neomacs_mixed.fast')\n"
            "_fast.__file__ = '/somewhere/fast.cpython-315t-darwin.so'\n"
            "sys.modules['neomacs_mixed.fast'] = _fast"
        )
        (site / "neomacs_mixed" / "__init__.py").write_text(source.replace("old source", "NEW"))
        ui.eval("await ed.extensions.reload('mixed')")
        state = value(
            ui.client, "(ed.extensions.get('mixed').state, ed.extensions.get('mixed').error)"
        )
        assert state == ("restart", "neomacs_mixed.fast is compiled and cannot be reloaded")
        ui.call("mixed.hello")
        assert ui.message == "old source"  # none of the new source was evaluated


@needs_uv
def test_what_was_replaced_is_reloaded_whatever_its_version_says(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    library(wheels, "shared", "1.0")
    extension(wheels, "same", requires=("shared",))
    extension(wheels, "other")
    env = install(tmp_path, "neomacs-same", "neomacs-other")
    site = env.site_packages()
    assert site is not None
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=8, cols=100) as ui:
        c = ui.client
        ui.call("same.hello")
        assert ui.message == "hello from same 0.1"
        # Its files are replaced with the version unchanged, as a git source's
        # are when it moves to another commit.
        init = site / "neomacs_same" / "__init__.py"
        init.write_text(init.read_text().replace("hello from same", "a later commit of same"))
        site_expr = f"__import__('pathlib').Path({str(site)!r})"
        assert (
            value(c, f"await ed.extensions.refresh({site_expr})") == []
        )  # nothing said to have changed
        ui.call("same.hello")
        assert ui.message == "hello from same 0.1"
        done = value(c, f"await ed.extensions.refresh({site_expr}, {{'neomacs-same'}})")
        assert done == ["reloaded same at 0.1"]
        ui.call("same.hello")
        assert ui.message == "a later commit of same 0.1"
        # A library under it changes: what is loaded is the old one, so it says to restart.
        done = value(c, f"await ed.extensions.refresh({site_expr}, {{'shared'}})")
        assert done == ["same uses shared, which changed; restart to take it up"]
        states = value(c, "{e.name: (e.state, e.error) for e in ed.extensions}")
        assert states == {
            "other": ("loaded", ""),
            "same": ("restart", "shared was upgraded under it"),
        }
        # One that nothing loaded is known to use: said, without accusing anyone.
        done = value(c, f"await ed.extensions.refresh({site_expr}, {{'unrelated'}})")
        assert done == ["a library changed; restart if a loaded extension uses it"]


# -- installing from inside the editor -----------------------------------------------------------


@needs_uv
def test_adding_and_removing_in_the_running_editor(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    library(wheels, "shared", "1.0")
    library(wheels, "shared", "2.0")
    extension(wheels, "a", requires=("shared>=1,<3",))
    extension(wheels, "b", requires=("shared>=2",), source=LAZY_SOURCE.replace("lazy", "b"),
              declarations=LAZY.replace("lazy", "b"))  # fmt: skip
    extension(wheels, "c", requires=("shared<2",))
    config(tmp_path / "config", wheels)
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=14, cols=100) as ui:
        c = ui.client

        def add(request: str, answer: str | None) -> None:
            ui.keys("M-x")
            ui.type("extensions.add")
            ui.keys("<return>")
            ui.type(request)
            ui.keys("<return>")
            if answer is not None:
                wait_for(lambda: ui.prompt is not None and ui.prompt.startswith("Install these?"))
                ui.type(answer)
                ui.keys("<return>")

        add("neomacs-a", "no")
        # It ran in the background, showed what it would do, and did nothing unasked.
        assert "Adding neomacs-a\n\nTo be added:\n  + neomacs-a 0.1\n  + shared 2.0\n" in value(
            c, "ed.buffers.get('*extensions*').text"
        )
        wait_for(lambda: ui.message == "Nothing was installed")
        assert not (tmp_path / "config" / "extensions.lock").exists()

        add("neomacs-a", "yes")
        wait_for(lambda: ui.message.startswith("Added neomacs-a"))
        assert ui.message == "Added neomacs-a; added a 0.1"
        # Usable at once, with no restart.
        ui.call("a.hello")
        assert ui.message == "hello from a 0.1"

        add("neomacs-b", "yes")
        wait_for(lambda: ui.message.startswith("Added neomacs-b"))
        assert value(c, "{e.name: e.state for e in ed.extensions}") == {"a": "loaded", "b": "known"}
        ui.call("b.hello")
        assert ui.message == "hello from b"

        add("neomacs-c", None)  # refused before anything is asked
        wait_for(lambda: ui.message.startswith("These extensions cannot be installed together."))
        assert ui.message.endswith("See *extensions*.")
        explained = value(c, "ed.buffers.get('*extensions*').text")
        assert __import__("re").search(r"depends? on shared>=2", explained)
        assert __import__("re").search(r"depends? on shared<2", explained)
        assert value(c, "sorted(e.name for e in ed.extensions)") == ["a", "b"]

        ui.keys("M-x")
        ui.type("extensions.remove")
        ui.keys("<return>")
        ui.type("neomacs-b")
        ui.keys("<return>")
        wait_for(lambda: ui.prompt is not None and ui.prompt.startswith("Install these?"))
        ui.type("yes")
        ui.keys("<return>")
        wait_for(lambda: ui.message.startswith("Removed neomacs-b"))
        assert value(c, "sorted(e.name for e in ed.extensions)") == ["a"]
        assert value(c, "[n for n in ed.commands if n.startswith('b.')]") == []
        assert value(c, f"[m for m in {MODS} if m.startswith('neomacs_b')]") == []
        ui.call("a.hello")
        assert ui.message == "hello from a 0.1"
        ui.call("extensions.list")
        assert value(c, "ed.buffers.get('*extensions*').text").startswith(
            "a  0.1  loaded  The a extension\n"
        )
    # And the next engine has what was installed, from the start.
    with running_engine(env=dirs(tmp_path)) as path, Client(path) as c:
        assert value(c, "{e.name: e.state for e in ed.extensions}") == {"a": "loaded"}


@needs_uv
def test_sync_turns_an_extension_off_before_its_files_are_taken_away(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    # Its clean-up needs a file of its own that it has not imported yet.
    source = (
        "import builtins\n"
        "def deactivate():\n"
        "    from . import late\n"
        "    builtins._cleaned_up = late.WORD\n"
    )
    extension(wheels, "tidy", source=source, extra={"late.py": "WORD = 'done properly'\n"})
    extension(wheels, "stays")
    install(tmp_path, "neomacs-tidy", "neomacs-stays")
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=12, cols=100) as ui:
        manifest = tmp_path / "config" / "extensions.toml"
        manifest.write_text(manifest.read_text().replace('neomacs-tidy = "*"\n', ""))
        ui.client.notify("engine.call", "extensions.sync", [])
        wait_for(lambda: ui.prompt is not None and ui.prompt.startswith("Install these?"))
        ui.type("yes")
        ui.keys("<return>")
        wait_for(lambda: ui.message.startswith("The environment matches extensions.toml"))
        assert ui.message == "The environment matches extensions.toml; removed tidy"
        assert value(ui.client, "__import__('builtins')._cleaned_up") == "done properly"
        assert value(ui.client, "[m for m in ed.messages if 'deactivating' in m]") == []
        assert value(ui.client, "sorted(e.name for e in ed.extensions)") == ["stays"]


@needs_uv
def test_installing_shows_in_the_task_list_and_leaves_the_editor_usable(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    extension(wheels, "a")
    config(tmp_path / "config", wheels)
    with running_engine(env=dirs(tmp_path)) as path, HeadlessUI(path, rows=10, cols=90) as ui:
        ui.client.notify("engine.call", "extensions.add", ["neomacs-a"])
        wait_for(lambda: ui.prompt is not None and ui.prompt.startswith("Install these?"))
        tasks = value(ui.client, "[(t.name, t.progress) for t in ed.tasks]")
        assert ("extensions: add neomacs-a", "resolving") in tasks
        ui.keys("C-g")  # not now
        ui.type("still editing")
        assert value(ui.client, "ed.buffer.text").endswith("still editing")


# -- fifty of them ---------------------------------------------------------------------------------


def startup_seconds(env: dict[str, str], runs: int) -> list[float]:
    times: list[float] = []
    for _ in range(runs):
        began = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "neomacs", "--stdio"],
            input=b'{"jsonrpc":"2.0","id":1,"method":"engine.eval","params":["len(list(ed.extensions))"]}\n'
            b'{"jsonrpc":"2.0","method":"engine.exit","params":[]}\n',
            capture_output=True, timeout=60, env={**__import__("os").environ, **env},
        )  # fmt: skip
        times.append(time.perf_counter() - began)
        assert proc.returncode == 0, proc.stderr
    return times


@needs_uv
def test_fifty_installed_extensions_add_no_measurable_startup_time(tmp_path: Path) -> None:
    import json

    wheels = tmp_path / "many" / "wheels"
    names = [f"n{i:02d}" for i in range(50)]
    for name in names:
        declarations = (
            f'[autoload]\ncommands = [{{ name = "{name}.hello", doc = "Hello from {name}." }}, '
            f'"{name}.other"]\nmodes = [{{ name = "{name}-mode", patterns = ["*.{name}"] }}]\n'
        )
        # Each would take a while to import, as real ones do.
        source = "import time\ntime.sleep(0.05)\n" + LAZY_SOURCE.replace("lazy", name)
        extension(wheels, name, source=source, declarations=declarations)
    install(tmp_path / "many", *[f"neomacs-{name}" for name in names])
    (tmp_path / "none" / "config").mkdir(parents=True)

    def measure(root: Path) -> tuple[float, int]:
        startup_seconds(dirs(root), 1)  # the first start writes the index
        times = startup_seconds(dirs(root), 9)
        probe = subprocess.run(
            [sys.executable, "-m", "neomacs", "--stdio"],
            input=b'{"jsonrpc":"2.0","id":1,"method":"engine.eval","params":'
            b'["(len(list(ed.extensions)), sum(n.startswith(\'n\') and \'.\' in n for n in ed.commands), '
            b"[m for m in __import__('sys').modules if m.startswith('neomacs_n')])\"]}\n"
            b'{"jsonrpc":"2.0","method":"engine.exit","params":[]}\n',
            capture_output=True, timeout=60, env={**__import__("os").environ, **dirs(root)},
        )  # fmt: skip
        count, commands, imported = eval(json.loads(probe.stdout)["result"])  # noqa: S307
        assert imported == []  # none of them was imported to start
        assert commands == count * 2
        return statistics.median(times), count

    without, zero = measure(tmp_path / "none")
    with_fifty, fifty = measure(tmp_path / "many")
    assert (zero, fifty) == (0, 50)
    # Importing them would cost 2.5 s.  What is allowed is the noise between runs.
    assert with_fifty - without < 0.05, (without, with_fifty)
    # And each is there to be used: the first use is what pays for the import.
    with (
        running_engine(env=dirs(tmp_path / "many")) as path,
        HeadlessUI(path, rows=8, cols=70) as ui,
    ):
        ui.call("n37.hello")
        assert ui.message == "hello from n37"
        assert value(ui.client, f"[m for m in {MODS} if m.startswith('neomacs_n')]") == [
            "neomacs_n37"
        ]
