"""Step 11: project settings from `.neomacs/settings.toml` (ENGINE 6, EXTENSIONS 8.1)."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.testing import Client
from neomacs.testing.headless import HeadlessUI

SETTINGS = """\
[options]
tab_width = 2
fill_column = 100
wrap = false
no_such_option = 1
indent_with_tabs = "yes"

[mode.python.options]
fill_column = 88

[mode.text.options]
tab_width = 6

[mode.python]
hooks = ["x"]

[keys]
"C-x" = "engine.exit"
"""


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=12, cols=70) as session:
        session.eval(f"_w = ed.frames[{session.frame_id}].window; ed.messages.clear()")
        yield session
        session.client.notify("ui.detach")


def project(root: Path, text: str = SETTINGS) -> Path:
    (root / ".neomacs").mkdir(parents=True)
    (root / ".neomacs" / "settings.toml").write_text(text)
    return root


def opts(ui: HeadlessUI, *names: str) -> tuple[Any, ...]:
    return tuple(value(ui.client, f"ed.buffer.options.origin({n!r})") for n in names)


def test_a_project_file_sets_the_options_that_opted_in(ui: HeadlessUI, tmp_path: Path) -> None:
    root = project(tmp_path / "proj")
    (root / "src").mkdir()
    (root / "src" / "notes.txt").write_text("\tx\n")
    (root / "src" / "code.py").write_text("\tx\n")
    (root / "src" / "data.bin").write_text("\tx\n")
    (tmp_path / "outside.txt").write_text("\tx\n")

    ui.open(root / "src" / "data.bin")  # fundamental mode: the general section
    assert opts(ui, "tab_width", "fill_column") == (("project", 2), ("project", 100))
    assert ui.view.info["tab_width"] == 2  # and the frontend is told
    # `wrap` did not opt in, so a project cannot set it.
    assert opts(ui, "wrap") == (("default", True),)

    ui.open(root / "src" / "code.py")  # its mode's section over the general one
    assert opts(ui, "tab_width", "fill_column") == (("project", 2), ("project", 88))
    ui.open(root / "src" / "notes.txt")
    assert opts(ui, "tab_width", "fill_column") == (("project", 6), ("project", 100))

    ui.open(tmp_path / "outside.txt")
    assert opts(ui, "tab_width", "fill_column") == (("default", 8), ("default", 80))


def test_what_is_skipped_is_said_once_for_the_file(ui: HeadlessUI, tmp_path: Path) -> None:
    root = project(tmp_path / "proj")
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("")
        ui.open(root / name)
    said = [m for m in value(ui.client, "ed.messages") if "settings.toml" in m]
    assert len(said) == 1
    message = said[0]
    assert message.startswith(f"{(root / '.neomacs' / 'settings.toml').resolve()}: skipped ")
    for part in (
        "options.wrap (not an option a project may set)",
        "options.no_such_option (no such option)",
        "options.indent_with_tabs (option indent_with_tabs must be bool",
        "mode.python.hooks",
        "keys",
    ):
        assert part in message
    assert "tab_width" not in message and "fill_column" not in message


def test_precedence_buffer_then_project_then_mode_then_global(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = project(tmp_path / "proj", "[options]\ntab_width = 2\n")
    (root / "code.py").write_text("x\n")
    (tmp_path / "loose.py").write_text("x\n")
    ui.eval("ed.options.tab_width = 7")
    try:
        ui.open(tmp_path / "loose.py")
        assert opts(ui, "tab_width") == (("mode", 4),)  # python's own default over the global
        ui.open(root / "code.py")
        assert opts(ui, "tab_width") == (("project", 2),)  # the project over the mode
        ui.eval("ed.buffer.options.tab_width = 5")
        assert opts(ui, "tab_width") == (("buffer", 5),)  # the buffer over all
        ui.eval("ed.buffer.options.unset('tab_width')")
        assert opts(ui, "tab_width") == (("project", 2),)
    finally:
        ui.eval("ed.options.unset('tab_width')")


def test_the_nearest_project_wins_and_they_do_not_merge(ui: HeadlessUI, tmp_path: Path) -> None:
    outer = project(tmp_path / "outer", "[options]\ntab_width = 2\nfill_column = 100\n")
    inner = project(outer / "vendor" / "inner", "[options]\ntab_width = 3\n")
    (inner / "x.bin").write_text("")
    ui.open(inner / "x.bin")
    assert opts(ui, "tab_width", "fill_column") == (("project", 3), ("default", 80))


def test_saving_the_settings_file_applies_it_to_open_buffers(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = project(tmp_path / "proj", "[options]\ntab_width = 2\n")
    (root / "a.bin").write_text("\tx\n")
    ui.open(root / "a.bin")
    ui.eval("_a = ed.buffer")
    ui.open(root / ".neomacs" / "settings.toml")
    ui.eval("_s = ed.buffer; _s.replace(0, len(_s), '[options]\\ntab_width = 5\\n')")
    assert value(ui.client, "_a.options.tab_width") == 2  # not until it is saved
    ui.keys("C-x", "C-s")
    assert value(ui.client, "_a.options.origin('tab_width')") == ("project", 5)


def test_reload_finds_changes_made_outside_and_new_projects(ui: HeadlessUI, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.bin").write_text("x\n")
    ui.open(root / "a.bin")
    assert opts(ui, "tab_width") == (("default", 8),)
    project(root, "[options]\ntab_width = 2\n")
    assert opts(ui, "tab_width") == (("default", 8),)  # nothing watches the disk
    ui.call("project.reload_settings")
    assert opts(ui, "tab_width") == (("project", 2),)
    assert ui.message.startswith("Read ") and "project settings file" in ui.message
    (root / ".neomacs" / "settings.toml").write_text("[options]\ntab_width = 9\n")
    ui.call("project.reload_settings")
    assert opts(ui, "tab_width") == (("project", 9),)
    (root / ".neomacs" / "settings.toml").unlink()
    ui.call("project.reload_settings")
    assert opts(ui, "tab_width") == (("default", 8),)


def test_saving_a_buffer_into_a_project_gives_it_the_projects_values(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = project(tmp_path / "proj", "[options]\ntab_width = 2\n")
    (tmp_path / "loose.bin").write_text("x\n")
    ui.open(tmp_path / "loose.bin")
    assert opts(ui, "tab_width") == (("default", 8),)
    ui.eval(f"ed.buffer.save({str(root / 'kept.bin')!r})")
    assert opts(ui, "tab_width") == (("project", 2),)


def test_a_file_that_does_not_parse_is_reported_and_sets_nothing(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = project(tmp_path / "proj", "[options\ntab_width = 2\n")
    (root / "a.bin").write_text("x\n")
    ui.open(root / "a.bin")
    assert opts(ui, "tab_width") == (("default", 8),)
    assert any("settings.toml" in m and "Error" not in m for m in value(ui.client, "ed.messages"))
    assert "settings.toml" in ui.message


def test_describe_option_names_the_project_file(ui: HeadlessUI, tmp_path: Path) -> None:
    root = project(tmp_path / "proj", "[options]\ntab_width = 2\n")
    (root / "a.bin").write_text("x\n")
    ui.open(root / "a.bin")
    ui.keys("M-x")
    ui.type("describe_option")
    ui.keys("<return>")
    ui.type("tab_width")
    ui.keys("<return>")
    text = value(ui.client, "ed.buffers.get('*help*').text")
    file = (root / ".neomacs" / "settings.toml").resolve()
    assert f"Value here: 2  (from the project file {file})" in text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes to make here")
def test_a_settings_file_that_is_a_pipe_does_not_stall_the_editor(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    (root / ".neomacs").mkdir(parents=True)
    os.mkfifo(root / ".neomacs" / "settings.toml")
    (root / "a.bin").write_text("x\n")
    done: list[bool] = []

    def visit() -> None:
        ui.open(root / "a.bin")
        done.append(True)

    opening = threading.Thread(target=visit, daemon=True)
    opening.start()
    opening.join(8)
    assert done == [True]
    assert opts(ui, "tab_width") == (("default", 8),)


def test_the_file_is_data_and_runs_nothing(ui: HeadlessUI, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    root = project(
        tmp_path / "proj",
        f'[options]\ntab_width = 2\n[commands]\nrun = "touch {marker}"\n[options.nested]\nx = 1\n',
    )
    (root / "a.bin").write_text("x\n")
    ui.open(root / "a.bin")
    assert not marker.exists()
    assert opts(ui, "tab_width") == (("project", 2),)
    assert value(ui.client, "sorted(n for n in ed.commands if 'run' == n)") == []
