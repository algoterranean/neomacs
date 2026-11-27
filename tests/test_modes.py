"""Stage 6: modes, highlighting from the syntax table, structure commands, search."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs import modes
from neomacs.testing import Client, running_engine
from neomacs.testing.headless import HeadlessUI

CODE = '''import os

def greet(name, times=2):
    """Say hello."""
    for i in range(times):  # loop
        print("hello", name)
    return None

class Thing(Base):
    value = 42

    @property
    def twice(self):
        return self.value * 2
'''


def test_mode_for_a_file_name_or_an_interpreter_line() -> None:
    assert modes.mode_for("tool.py").name == "python"
    assert modes.mode_for("notes.txt").name == "text"
    assert modes.mode_for("README").name == "text"
    assert modes.mode_for("tool", "#!/usr/bin/env python3").name == "python"
    assert modes.mode_for("tool", "#!/usr/bin/python3.15 -u").name == "python"
    assert modes.mode_for("tool", "#!/bin/sh").name == "fundamental"
    assert modes.mode_for(None).name == "fundamental"


def test_keymaps_defaults_and_syntax_are_inherited() -> None:
    python = modes.modes["python"]
    assert [k.name for k in python.keymaps()] == ["python", "prog"]
    assert (
        python.syntax.line_comment == "#" and python.syntax.brackets == modes.Mode.syntax.brackets
    )
    assert modes.modes["text"].syntax.is_word("'") and not python.syntax.is_word("'")
    assert issubclass(python, modes.ProgMode) and issubclass(modes.ProgMode, modes.FundamentalMode)


def _tree_sitter_works() -> bool:
    """Whether an engine can load the parser and the Python grammar."""
    probe = (
        "import sys\n"
        "try:\n"
        "    import tree_sitter, tree_sitter_python\n"
        "    tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    env = dict(os.environ, PYTHON_GIL="0")
    return (
        subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True).returncode == 0
    )


# Everything below is run twice: with the syntax table alone, which is what an
# installation without tree-sitter has, and with a parse tree.
@pytest.fixture(scope="module", params=["table", "tree"])
def engine(request: pytest.FixtureRequest) -> Iterator[tuple[Path, bool]]:
    tree = request.param == "tree"
    if tree and not _tree_sitter_works():
        pytest.skip("tree-sitter and its Python grammar are not installed")
    # PYTHON_GIL=0 keeps the lock off when the parser's extension is loaded.
    env = {"PYTHON_GIL": "0"} if tree else {}
    with running_engine(env=env) as path:
        with Client(path) as c:
            c.eval(f"ed.options.tree_sitter = {'always' if tree else 'never'!r}")
        yield path, tree


@pytest.fixture
def tree(engine: tuple[Path, bool]) -> bool:
    """Whether this run has a parse tree."""
    return engine[1]


@pytest.fixture
def ui(engine: tuple[Path, bool], tmp_path: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine[0], rows=20, cols=70) as session:
        path = tmp_path / "sample.py"
        path.write_text(CODE)
        session.eval(f"_w = ed.frames[{session.frame_id}].window")
        session.open(path)
        session.eval("_b = _w.buffer")
        has_tree = eval(
            session.client.eval(
                "_b.syntax.tree is not None and _b.syntax.tree.ensure() is not None"
            )
        )  # noqa: S307
        assert has_tree is engine[1]
        yield session
        session.eval("ed.buffers.kill(_b); _w.show(ed.buffers.get('*scratch*'))")
        session.client.notify("ui.detach")


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def faces_on(ui: HeadlessUI, line: int) -> list[tuple[str, str]]:
    """(text, face name) for each run of LINE as the frontend holds it."""
    names = value(ui, "['+'.join(n) for n in ed.faces._names]")
    runs = (ui.view.line(line) or [{}, []])[1]
    return [(run[1], names[run[2]] if run[2] else "") for run in runs]


def at(ui: HeadlessUI, needle: str, offset: int = 0) -> None:
    ui.eval(f"_p = _b.text.index({needle!r}) + {offset}; _w.selections.set([(_p, _p)])")
    ui.idle()


def test_a_python_file_gets_python_mode_and_its_defaults(ui: HeadlessUI) -> None:
    assert value(ui, "(_b.mode.name, _b.options.tab_width, _b.options.auto_indent)") == (
        "python",
        4,
        True,
    )
    assert ui.modeline.rstrip().endswith("(python auto-pair)")
    assert value(ui, "(ed.options.tab_width, ed.buffers.get('*messages*').options.tab_width)") == (
        8,
        8,
    )


def test_highlighting_from_the_table(ui: HeadlessUI) -> None:
    assert faces_on(ui, 1) == [("import", "keyword"), (" os", "")]
    assert faces_on(ui, 3) == [
        ("def", "keyword"), (" ", ""), ("greet", "function"), ("(name, times=", ""), ("2", "number"), ("):", ""),
    ]  # fmt: skip
    assert faces_on(ui, 4) == [("    ", ""), ('"""Say hello."""', "string")]
    assert faces_on(ui, 5)[-1] == ("# loop", "comment")
    assert ("None", "constant") in faces_on(ui, 7)
    assert faces_on(ui, 9)[:3] == [("class", "keyword"), (" ", ""), ("Thing", "type")]
    assert faces_on(ui, 12) == [("    ", ""), ("@property", "decorator")]


def test_opening_a_string_recolours_the_lines_below_and_closing_it_puts_them_back(
    ui: HeadlessUI, tree: bool
) -> None:
    before = [faces_on(ui, n) for n in range(5, 9)]
    ui.eval("_b.insert(_b.line_start(5), 'x = \\'\\'\\'\\n')")
    ui.idle()
    if tree:
        # The parser recovers from the unfinished string in its own way; what
        # matters is that lines whose text did not change were sent again.
        assert [faces_on(ui, n) for n in range(6, 10)] != before
    else:
        assert faces_on(ui, 6) == [("    for i in range(times):  # loop", "string")]
        assert faces_on(ui, 9) == []  # an empty line
        assert faces_on(ui, 10) == [("class Thing(Base):", "string")]
    ui.eval("_b.delete(_b.line_start(5), _b.line_start(6))")
    ui.idle()
    assert [faces_on(ui, n) for n in range(5, 9)] == before


def test_highlighting_is_cached_per_line(ui: HeadlessUI, tree: bool) -> None:
    if tree:
        pytest.skip("the cache is the table highlighter's; a tree answers from itself")
    ui.eval("_h = _b.locals['_highlighter']; _n = _h.computed")
    at(ui, "hello")
    ui.type("X")
    assert value(ui, "_h.computed - _n") == 1  # only the line that changed
    ui.keys("C-/")
    assert value(ui, "_h.computed - _n") == 1  # and its old text was still known


def test_switching_mode_with_its_command(ui: HeadlessUI) -> None:
    ui.call("text_mode")
    assert value(ui, "_b.mode.name") == "text" and faces_on(ui, 1) == [("import os", "")]
    assert value(ui, "_b.options.tab_width") == 8
    ui.call("python_mode")
    assert faces_on(ui, 1)[0] == ("import", "keyword")
    ui.call("auto_pair_mode")
    assert ui.message == "auto-pair mode off" and "auto-pair" not in ui.modeline
    ui.call("auto_pair_mode")
    assert ui.message == "auto-pair mode on"


def test_expression_motion_and_matching_bracket(ui: HeadlessUI) -> None:
    at(ui, "print(")
    ui.keys("C-M-f")
    assert value(ui, "_b[_w.cursor.pos - 5 : _w.cursor.pos]") == "print"
    ui.keys("C-M-f")  # the bracketed group
    assert value(ui, "_b[_w.cursor.pos - 15 : _w.cursor.pos]") == '("hello", name)'
    ui.keys("C-M-b")
    assert value(ui, "_b[_w.cursor.pos]") == "("
    ui.keys("C-M-f", "M-]")  # from just after the closer, to its partner
    assert value(ui, "_b[_w.cursor.pos : _w.cursor.pos + 8]") == '("hello"'
    at(ui, '"hello"', 3)
    ui.keys("C-M-u")
    assert value(ui, "_b[_w.cursor.pos : _w.cursor.pos + 8]") == '("hello"'
    # The bracket at the cursor and its partner are marked.
    assert value(ui, "sorted(s.start for s in _b.layer('bracket-match'))") == value(
        ui, "[_w.cursor.pos, _b.syntax.matching_bracket(_w.cursor.pos)]"
    )
    ui.keys("C-a")
    assert value(ui, "len(_b.layer('bracket-match'))") == 0


def test_expand_and_shrink_selection(ui: HeadlessUI, tree: bool) -> None:
    if tree:
        # Node by node: the word, the string, the arguments, the call, its
        # statement, the loop's body, the loop, the function's body, the function.
        at(ui, '"hello"', 3)
        seen: list[str] = []
        while not seen or seen[-1] != CODE:
            ui.keys("C-=")
            seen.append(value(ui, "_w.selections.primary.text"))
            assert len(seen) < 20
        assert seen[:4] == ["hello", '"hello"', '("hello", name)', 'print("hello", name)']
        loop = next(text for text in seen if text.startswith("for i in range"))
        assert loop.endswith('print("hello", name)')
        function = next(text for text in seen if text.startswith("def greet"))
        assert function.endswith("return None") and seen.index(function) > seen.index(loop)
        assert all(a in b and a != b for a, b in zip(seen, seen[1:], strict=False))
        for expected in reversed(seen[:-1]):
            ui.keys("C-M-=")
            assert value(ui, "_w.selections.primary.text") == expected
        return
    at(ui, '"hello"', 3)
    seen = []
    for _ in range(4):
        ui.keys("C-=")
        seen.append(value(ui, "_w.selections.primary.text"))
    assert seen[0] == "hello"
    assert seen[1] == '("hello", name)'
    assert seen[2] == '        print("hello", name)'
    assert seen[3].startswith("def greet") and seen[3].endswith("return None")
    ui.keys("C-=")
    assert ui.message == "Nothing larger to select"
    for expected in reversed(seen[:3]):
        ui.keys("C-M-=")
        assert value(ui, "_w.selections.primary.text") == expected
    ui.keys("C-M-=")
    assert value(ui, "_w.selections.primary.empty") is True


def test_toggle_comment(ui: HeadlessUI) -> None:
    ui.eval("_w.selections.set([(_b.line_start(5), _b.line_start(7))])")
    ui.keys("M-;")
    assert value(ui, "[_b.line_text(n) for n in (5, 6, 7)]") == [
        "    # for i in range(times):  # loop",
        '    #     print("hello", name)',
        "    return None",
    ]
    assert faces_on(ui, 6)[-1][1] == "comment"
    ui.keys("M-;")
    assert value(ui, "_b.text") == CODE
    at(ui, "value = 42")
    ui.keys("M-;")
    assert value(ui, "_b.line_text(10)") == "    # value = 42"
    ui.keys("C-/")


def test_shipped_queries_compile(ui: HeadlessUI, tree: bool) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    assert value(ui, "__import__('neomacs.syntax.tree').syntax.tree._failures") == {}
    names = value(
        ui, "sorted({c for _, _, c, _ in _b.syntax.tree.captures('textobjects', 0, len(_b))})"
    )
    assert names == ["class", "function", "parameter", "statement", "string", "comment"] or set(names) >= {
        "class", "function", "parameter", "statement",
    }  # fmt: skip
    assert value(ui, "[_b[a:b].split('(')[0] for a, b in _b.syntax.units('function', 0, len(_b))]") == [
        "def greet", "@property\n    def twice",
    ]  # fmt: skip
    assert value(ui, "[_b[a:b] for a, b in _b.syntax.units('parameter', 0, _b.line_end(3))]") == [
        "name", "times=2",
    ]  # fmt: skip


def test_a_method_is_a_definition_when_there_is_a_tree(ui: HeadlessUI, tree: bool) -> None:
    at(ui, "self.value")
    ui.keys("C-M-a")
    line = value(ui, "_b.line_text(_b.line_of(_w.cursor.pos))")
    # With a tree: the method, from its decorator.  Without: the top-level block.
    assert line == ("    @property" if tree else "class Thing(Base):")
    ui.keys("C-M-h")
    selected = value(ui, "_w.selections.primary.text")
    assert selected.startswith("@property" if tree else "class Thing")
    assert selected.endswith("return self.value * 2")


def test_definitions(ui: HeadlessUI) -> None:
    at(ui, "hello")
    ui.keys("C-M-a")
    assert value(ui, "_b.line_of(_w.cursor.pos), _w.cursor.column") == (3, 0)
    ui.keys("C-M-e")
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 7
    ui.keys("C-M-e")  # the next one
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 14
    ui.keys("C-M-a")
    assert value(ui, "_b.line_text(_b.line_of(_w.cursor.pos))") == "class Thing(Base):"
    ui.keys("C-M-h")
    assert value(ui, "_w.selections.primary.text").startswith("class Thing") is True


def test_indentation_and_new_lines(ui: HeadlessUI) -> None:
    ui.eval(
        "_b.replace(0, len(_b), 'def f(a,\\n      b):\\nx = [1,\\n2]\\nif a:\\nreturn 1\\nelse:\\npass\\ny = 2\\n')"
    )
    for line in range(2, 10):
        ui.eval(f"_w.selections.set([(_b.line_start({line}),) * 2])")
        ui.keys("<tab>")
    assert value(ui, "_b.text.split('\\n')") == [
        "def f(a,", "    b):", "    x = [1,", "        2]", "    if a:", "        return 1", "    else:",
        "        pass", "    y = 2", "",
    ]  # fmt: skip
    # <return> after a colon indents; repeating <tab> steps back a level.
    ui.eval("_b.replace(0, len(_b), 'while True:'); _w.selections.set([(len(_b),) * 2])")
    ui.keys("<return>")
    ui.type("if x:")
    ui.keys("<return>")
    ui.type("break")
    ui.keys("<return>")
    ui.type("done")
    assert value(ui, "_b.text") == "while True:\n    if x:\n        break\n    done"
    ui.keys("<tab>")
    assert value(ui, "_b.line_text(4)") == "    done"
    ui.keys("<tab>")
    assert value(ui, "_b.line_text(4)") == "done"


def test_pairs(ui: HeadlessUI) -> None:
    ui.eval("_b.replace(0, len(_b), ''); _w.selections.set([(0, 0)])")
    ui.type("f(")
    assert value(ui, "(_b.text, _w.cursor.pos)") == ("f()", 2)
    ui.type("[")
    ui.type('"')
    assert value(ui, "(_b.text, _w.cursor.pos)") == ('f([""])', 4)
    ui.type("it's")  # no pairing inside a string
    ui.type('"')  # types over the closing quote
    ui.type("]")
    ui.type(")")
    assert value(ui, "(_b.text, _w.cursor.pos)") == ('f(["it\'s"])', 11)
    ui.type(" x = (")
    ui.keys("<backspace>")  # removes both halves of an empty pair
    assert value(ui, "_b.text") == 'f(["it\'s"]) x = '
    ui.eval("_b.replace(0, len(_b), 'wrap me'); _w.selections.set([(0, 4)])")
    ui.type("(")
    assert value(ui, "(_b.text, _w.selections.primary.text)") == ("(wrap) me", "wrap")
    ui.eval("_b.replace(0, len(_b), 'ab'); _w.selections.set([(1, 1)])")
    ui.type("(")  # in front of text: just the one character
    assert value(ui, "_b.text") == "a(b"


def test_incremental_search(ui: HeadlessUI) -> None:
    at(ui, "import")
    ui.keys("C-s")
    ui.type("name")
    assert ui.message == "Search: name  [1/2]" or ui.model.keys == "Search: name  [1/2]"
    assert value(ui, "(_b.line_of(_w.cursor.pos), _w.selections.primary.text)") == (3, "name")
    assert value(ui, "sorted((s.get('face'), _b.line_of(s.start)) for s in _b.layer('search'))") == [
        ("match", 6), ("match-current", 3),
    ]  # fmt: skip
    ui.keys("C-s")
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 6
    ui.keys("C-s")  # wraps
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 3
    ui.keys("C-r")
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 6
    ui.keys("<backspace>", "<backspace>")
    assert value(ui, "_w.selections.primary.text") == "na"
    ui.type("zz")
    assert (ui.model.keys or ui.message).startswith("Failing search: nazz")
    ui.keys("C-g")  # back where it began
    assert value(ui, "(_b.line_of(_w.cursor.pos), len(_b.layer('search')))") == (1, 0)
    # Another key ends the search and then does what it does.
    ui.keys("C-s")
    ui.type("times")
    ui.keys("C-e")
    assert value(ui, "(_b.line_of(_w.cursor.pos), _w.cursor.pos == _b.line_end(3))") == (3, True)
    ui.keys("M-,")  # the search left a place to come back to
    assert value(ui, "_b.line_of(_w.cursor.pos)") == 1
    # Case is ignored until a capital is typed.
    ui.keys("C-s")
    ui.type("thing")
    assert value(ui, "_w.selections.primary.text") == "Thing"
    ui.type("X")
    assert (ui.model.keys or ui.message).startswith("Failing search")
    ui.keys("C-g")
    # C-s C-s searches again for the last thing found.
    ui.keys("C-s", "C-s")
    assert value(ui, "_w.selections.primary.text") == "times"
    ui.keys("<return>")


def test_a_cursor_on_every_match_of_a_word(ui: HeadlessUI) -> None:
    at(ui, "name, times", 1)
    ui.keys("C-c", "m")
    assert ui.message == "2 selections"
    assert value(ui, "[(_b.line_of(s.start), s.text) for s in _w.selections]") == [
        (3, "name"), (6, "name"),
    ]  # fmt: skip
    ui.type("who")
    assert value(ui, "(_b.text.count('who'), _b.text.count('name'))") == (2, 0)
    ui.keys("C-g", "C-/")
    assert value(ui, "_b.text") == CODE
    # Whole words only: "value" is not found inside "values".
    ui.eval("_b.insert(len(_b), 'values = value\\n')")
    at(ui, "value = 42")
    ui.keys("C-c", "m")
    assert value(ui, "[_b.line_of(s.start) for s in _w.selections]") == [10, 14, 15]
    ui.keys("C-g")
    # One at a time: the word, then each next occurrence.
    at(ui, "times=2")
    ui.keys("M-n")
    assert value(ui, "_w.selections.ranges() == [(_p, _p + 5)]") is True
    ui.keys("M-n")
    assert value(ui, "[_b.line_of(s.start) for s in _w.selections]") == [3, 5]
    ui.keys("M-n")
    assert ui.message == "No more matches"
    ui.keys("C-g")
    # From a search: every match becomes a selection.
    ui.keys("C-s")
    ui.type("thing")
    ui.keys("M-a")
    assert value(ui, "[s.text for s in _w.selections]") == ["Thing"]


# -- only with a parse tree ---------------------------------------------------------


def test_positions_are_converted_between_characters_and_bytes(ui: HeadlessUI, tree: bool) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    ui.eval(
        "_b.replace(0, len(_b), 'héllo = \"çà et là\"  # naïve\\ndef fête(x):\\n    return x\\n')"
    )
    ui.idle()
    assert faces_on(ui, 1) == [
        ("héllo = ", ""),
        ('"çà et là"', "string"),
        ("  ", ""),
        ("# naïve", "comment"),
    ]
    assert faces_on(ui, 2)[:3] == [("def", "keyword"), (" ", ""), ("fête", "function")]
    assert value(ui, "_b.syntax.kind_at(_b.text.index('et'))") == "string"
    assert value(ui, "_b[slice(*_b.syntax.unit_at(_b.text.index('return'), 'function'))]") == (
        "def fête(x):\n    return x"
    )


def test_the_scanner_answers_where_the_code_does_not_parse(ui: HeadlessUI, tree: bool) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    ui.eval("_b.replace(0, len(_b), 'ok = (1, 2)\\nbroken = foo(bar[1, {2: 3}\\n')")
    ui.idle()
    assert value(ui, "_b.syntax.tree.ensure().root_node.has_error") is True
    assert value(ui, "_b.syntax._trusted_tree(0, 0) is not None") is True  # the first line parsed
    at(ui, "{2")
    assert value(ui, "_b.syntax._trusted_tree(_w.cursor.pos, _w.cursor.pos) is None") is True
    ui.keys("M-]")  # the brace's partner is still found, by the scanner
    assert value(ui, "_b[_w.cursor.pos - 1]") == "}"
    assert value(ui, "_b.syntax.unit_at(_w.cursor.pos, 'function')") is None


def test_a_large_file_is_first_parsed_off_the_owner_thread(
    ui: HeadlessUI, tree: bool, tmp_path: Path
) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    big = tmp_path / "big.py"
    big.write_text(
        "".join(f"def function_{n}(a, b):\n    return a + b  # {n}\n\n" for n in range(6000))
    )
    ui.client.call("engine.call", "files.find", [str(big)])
    # At once, before the parse can have finished: edit the top of the file.
    ui.client.eval(
        "_big = next(b for b in ed.buffers if b.name == 'big.py'); _t = _big.syntax.tree\n"
        "_was_parsing = _t._background is not None and _t.ensure() is None\n"
        "_scanner_kind = _big.syntax.kind_at(_big.text.index('# 0'))\n"
        "_big.insert(0, 'import os  # added while parsing\\n')"
    )
    assert value(ui, "(_was_parsing, _scanner_kind)") == (True, "comment")
    deadline = time.monotonic() + 20
    while value(ui, "_t._background is not None"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    ui.idle()
    assert value(
        ui, "(_t.background_parses, _t.ensure() is not None, _t.ensure().root_node.has_error)"
    ) == (1, True, False)
    assert value(
        ui, "_big[slice(*_big.syntax.unit_at(_big.text.index('# 5999'), 'function'))]"
    ) == ("def function_5999(a, b):\n    return a + b  # 5999")
    view = ui.view_showing("big.py")
    assert view is not None
    names = value(ui, "['+'.join(n) for n in ed.faces._names]")
    first = [(run[1], names[run[2]] if run[2] else "") for run in (view.line(1) or [{}, []])[1]]
    assert first == [("import", "keyword"), (" os  ", ""), ("# added while parsing", "comment")]
    ui.eval("ed.buffers.kill(_big)")


# -- from review ---------------------------------------------------------------------


def test_adding_matches_during_a_search_keeps_the_earlier_ones(ui: HeadlessUI) -> None:
    ui.eval("_b.replace(0, len(_b), 'foo bar foo baz foo'); _w.selections.set([(0, 0)])")
    ui.keys("C-s")
    ui.type("foo")
    ui.keys("M-n")
    assert value(ui, "_w.selections.ranges()") == [(0, 3), (8, 11)]
    ui.keys("M-n")
    assert value(ui, "_w.selections.ranges()") == [(0, 3), (8, 11), (16, 19)]
    ui.keys("<return>")
    ui.type("X")
    assert value(ui, "_b.text") == "X bar X baz X"
    ui.keys("C-g", "C-/")


def test_search_ignores_case_whatever_else_is_in_the_buffer(ui: HeadlessUI) -> None:
    # Lower-casing "İ" gives two characters, which once switched folding off for the whole buffer.
    ui.eval("_b.replace(0, len(_b), 'İ FOO and Foo'); _w.selections.set([(0, 0)])")
    assert value(ui, "__import__('neomacs.std.search').std.search.find_all(_b, 'foo')") == [
        (2, 5),
        (10, 13),
    ]
    ui.keys("C-s")
    ui.type("foo")
    assert value(ui, "_w.selections.primary.text") == "FOO"
    ui.keys("C-g")


def test_tab_inside_a_multi_line_string_changes_nothing(ui: HeadlessUI) -> None:
    text = 'def f():\n    """Summary.\n\nflush left on purpose\n    """\n'
    ui.eval(f"_b.replace(0, len(_b), {text!r}); _w.selections.set([(_b.line_start(4),) * 2])")
    ui.keys("<tab>")
    assert value(ui, "_b.text") == text


def test_killing_the_buffer_in_a_panel_leaves_the_document_alone(ui: HeadlessUI) -> None:
    ui.call("repl")
    assert value(ui, f"ed.frames[{ui.frame_id}].focus.buffer.name") == "*repl*"
    ui.keys("C-x", "k")
    assert value(
        ui, "(ed.buffers.get('*repl*') is None, _b.name in [b.name for b in ed.buffers])"
    ) == (True, True)
    assert value(ui, f"(_w.buffer is _b, ed.frames[{ui.frame_id}].focus is _w)") == (True, True)
    assert all(tab["title"] != "*repl*" for tab in ui.panels()["bottom"]["tabs"])


def test_the_parser_setting_is_obeyed_per_buffer_and_when_it_changes(
    ui: HeadlessUI, tree: bool
) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    assert value(ui, "_b.syntax.tree is not None") is True
    # Off for this buffer, though the grammar is loaded and cached for others.
    ui.eval("_b.options.tree_sitter = 'never'")
    assert value(ui, "(_b.syntax.tree is None, _b.locals['_highlighter'].from_tree)")[0] is True
    assert faces_on(ui, 1)[0] == ("import", "keyword")  # the table highlighter took over
    ui.eval("_b.options.tree_sitter = 'always'")
    assert value(ui, "_b.syntax.tree is not None and _b.syntax.tree.ensure() is not None") is True


def test_without_a_highlight_query_the_table_highlights(ui: HeadlessUI, tree: bool) -> None:
    if not tree:
        pytest.skip("needs the grammar")
    ui.eval(
        "from neomacs.syntax import tree as _tm\n"
        "_saved = _tm._queries.get(('python', 'highlights')); _tm._queries[('python', 'highlights')] = None\n"
        "_b.invalidate(0, len(_b))"
    )
    try:
        assert faces_on(ui, 3)[:3] == [("def", "keyword"), (" ", ""), ("greet", "function")]
    finally:
        ui.eval("_tm._queries[('python', 'highlights')] = _saved; _b.invalidate(0, len(_b))")
