"""Step 12, F5: themes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.display import faces
from neomacs.testing import Client, RemoteError
from neomacs.testing.headless import HeadlessUI


def value(client: Client, expr: str) -> Any:
    return eval(client.eval(expr))  # noqa: S307 - a repr from our own engine


@pytest.fixture
def ui(engine_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine_path, rows=10, cols=60) as session:
        session.eval(
            f"_w = ed.frames[{session.frame_id}].window; _b = ed.buffers.new('code.py'); "
            "_b.set_mode('python'); _b.insert(0, 'def f():\\n    return \"s\"\\n'); _w.show(_b)"
        )
        yield session
        session.eval("ed.options.theme = 'dark'; ed.faces.own.clear(); ed.faces.load_theme('dark')")
        session.client.notify("ui.detach")


def colours(ui: HeadlessUI) -> dict[str, Any]:
    """What the frontend has been told: the default face, a keyword's, and the chrome's."""
    model = ui.model
    line = ui.view.line(1)
    assert line is not None
    keyword = next(run[2] for run in line[1] if run[1] == "def")
    return {
        "default": model.faces[0],
        "keyword": model.faces[keyword],
        "popup": model.faces[model.ui_faces["popup"]],
        "modeline": model.faces[model.ui_faces["modeline"]],
    }


def test_both_themes_give_every_face_the_frontend_draws_with() -> None:
    assert set(faces.themes) >= {"dark", "light"}
    assert set(faces.DARK) == set(faces.LIGHT)
    for theme in (faces.DARK, faces.LIGHT):
        assert set(faces.UI_FACES) <= set(theme)
        assert all(set(attrs) <= faces.ATTRIBUTES for attrs in theme.values())


def test_selecting_a_theme_recolours_everything_and_sends_no_text_again(ui: HeadlessUI) -> None:
    dark = colours(ui)
    assert dark["default"] == {"fg": "#d4d4d4", "bg": "#1e1e1e"}
    assert dark["keyword"] == {"fg": "#c586c0"} and dark["popup"]["bg"] == "#252526"
    ui.eval("_lines_before = _w._held")
    ui.keys("M-x")
    ui.type("theme.select")
    ui.keys("<return>")
    assert ui.candidates() == ["dark", "light"]
    ui.type("light")
    ui.keys("<return>")
    assert ui.message == "Theme: light"
    light = colours(ui)
    assert light["default"] == {"fg": "#1f2328", "bg": "#ffffff"}
    assert light["keyword"] == {"fg": "#a626a4"}
    assert light["popup"] == {"fg": "#1f2328", "bg": "#f6f8fa"}
    assert light["modeline"] == {"fg": "#1f2328", "bg": "#d0d7de"}
    assert value(ui.client, "(ed.options.theme, ed.faces.theme)") == ("light", "light")
    # The same lines are held, with the same face ids: only the faces were sent.
    assert value(ui.client, "_w._held == _lines_before") is True
    assert ui.text_rows_shown()[0] == " 1 def f():"


def test_a_second_frame_changes_too_and_a_new_one_starts_in_the_theme(
    ui: HeadlessUI, engine_path: Path
) -> None:
    with HeadlessUI(engine_path, rows=6, cols=40) as second:
        ui.eval("ed.options.theme = 'light'")
        second.idle()
        assert second.model.faces[0] == {"fg": "#1f2328", "bg": "#ffffff"}
    with HeadlessUI(engine_path, rows=6, cols=40) as third:
        assert third.model.faces[0] == {"fg": "#1f2328", "bg": "#ffffff"}


def test_a_face_the_user_defined_keeps_its_attributes_across_themes(ui: HeadlessUI) -> None:
    ui.eval("ed.faces.define('keyword', fg='#ff0000', weight='bold')")
    assert colours(ui)["keyword"] == {"fg": "#ff0000", "weight": "bold"}
    ui.eval("ed.options.theme = 'light'")
    after = colours(ui)
    assert after["keyword"] == {"fg": "#ff0000", "weight": "bold"}
    assert after["default"]["bg"] == "#ffffff"


def test_a_theme_can_be_defined_and_goes_with_whoever_defined_it(ui: HeadlessUI) -> None:
    ui.eval(
        "from neomacs.display.faces import define_theme\n"
        "define_theme('sepia', {'default': {'fg': '#433422', 'bg': '#f4ecd8'}}, base='light')"
    )
    ui.eval("ed.options.theme = 'sepia'")
    sepia = colours(ui)
    assert sepia["default"] == {"fg": "#433422", "bg": "#f4ecd8"}
    assert sepia["keyword"] == {"fg": "#a626a4"}  # from the theme it was built on
    with pytest.raises(RemoteError, match="unknown face attributes in default"):
        ui.client.eval("define_theme('bad', {'default': {'colour': 'red'}})")
    with pytest.raises(RemoteError, match="no theme called nowhere"):
        ui.client.eval("define_theme('bad', {}, base='nowhere')")
    ui.eval("ed.options.theme = 'dark'")
    ui.eval("__import__('neomacs.ownership').ownership.ownership.release('neomacs_scratch')")
    assert (
        value(ui.client, "'sepia' in __import__('neomacs.display.faces').display.faces.themes")
        is False
    )


def test_a_theme_that_does_not_exist_is_refused(ui: HeadlessUI) -> None:
    with pytest.raises(RemoteError, match="not a valid value for option theme"):
        ui.client.eval("ed.options.theme = 'no-such-theme'")
    assert value(ui.client, "ed.faces.theme") == "dark"
    ui.keys("M-x")
    ui.type("theme.select")
    ui.keys("<return>")
    ui.type("no-such-theme")
    ui.keys("<return>")
    assert ui.message == "There is no theme called no-such-theme"
