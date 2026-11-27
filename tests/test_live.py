"""Stage 4: live evaluation (ENGINE 9)."""

from __future__ import annotations

import asyncio
import functools
import inspect
import traceback
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neomacs.live import source
from neomacs.live.evaluate import NO_VALUE, Evaluation, evaluate
from neomacs.testing import running_engine
from neomacs.testing.headless import HeadlessUI

# -- the pipeline and patching, with no editor -------------------------------------


def run(text: str, ns: dict[str, Any], **kw: Any) -> Evaluation:
    return asyncio.run(evaluate(text, ns, **kw))


def module(name: str = "live_demo") -> dict[str, Any]:
    ns = types.ModuleType(name).__dict__
    ns["functools"] = functools
    return ns


def test_value_of_a_trailing_expression_and_modes() -> None:
    ns = module()
    assert run("x = 2\nx * 21", ns).value == 42
    assert run("x = 3", ns).value is NO_VALUE
    assert run("x * 2", ns, mode="exec").value is NO_VALUE
    assert run("", ns).value is NO_VALUE


def test_top_level_await() -> None:
    ns = module()
    assert run("import asyncio\ny = await asyncio.sleep(0, 'a')\ny + 'b'", ns).value == "ab"
    assert run("await asyncio.sleep(0, 5)", ns).value == 5


def test_a_function_is_patched_in_place() -> None:
    ns = module()
    run("def f(x, y=1):\n    'old'\n    return x + y", ns)
    held = ns["f"]
    result = run("def f(x, y=10, *, z=100):\n    'new'\n    return x + y + z", ns)
    assert ns["f"] is held and held(1) == 111 and held.__doc__ == "new"
    assert result.rebound == []
    assert str(inspect.signature(held)) == "(x, y=10, *, z=100)"


def test_async_and_generator_functions_are_patched() -> None:
    ns = module()
    run("async def a():\n    return 1\ndef g():\n    yield 1", ns)
    a, g = ns["a"], ns["g"]
    assert run("async def a():\n    return 2\ndef g():\n    yield 2", ns).rebound == []
    assert asyncio.run(a()) == 2 and list(g()) == [2]


def test_a_changed_closure_falls_back_to_rebinding_with_a_reason() -> None:
    ns = module()
    run(
        "def make():\n    n = 1\n    def inner():\n        return n\n    return inner\nf = make()",
        ns,
    )
    run(
        "def wrap(fn):\n    def w():\n        return fn()\n    return w\n@wrap\ndef g():\n    return 1",
        ns,
    )
    old = ns["g"]
    # Same decorator: the wrapper closes over the same names, so it is patched.
    result = run("@wrap\ndef g():\n    return 2", ns)
    assert result.rebound == [] and ns["g"] is old
    # The decorator is dropped: the old wrapper cannot become the plain function.
    result = run("def g():\n    return 3", ns)
    assert [name for name, _ in result.rebound] == ["g"] and "closes over" in result.rebound[0][1]
    assert ns["g"] is not old and ns["g"]() == 3


def test_decorated_with_wraps_patches_down_the_chain() -> None:
    ns = module()
    deco = "def deco(fn):\n    @functools.wraps(fn)\n    def wrapper(*a):\n        return ('wrapped', fn(*a))\n    return wrapper\n"
    run(deco + "@deco\ndef f():\n    return 1", ns)
    held, inner = ns["f"], ns["f"].__wrapped__
    result = run("@deco\ndef f():\n    return 2", ns)
    assert result.rebound == []
    assert (
        ns["f"] is held and held() == ("wrapped", 2) and held.__wrapped__ is inner and inner() == 2
    )


def test_a_class_is_patched_in_place() -> None:
    ns = module()
    run(
        "class C:\n    kind = 'old'\n    def m(self):\n        return 'old'\n    def gone(self):\n        return 1\n"
        "    @property\n    def p(self):\n        return 'old'\n"
        "class D(C):\n    def m(self):\n        return 'D ' + super().m()",
        ns,
    )
    C, D = ns["C"], ns["D"]
    c, d, bound = C(), D(), C().m
    result = run(
        "class C:\n    kind = 'new'\n    def m(self):\n        return 'new'\n    def added(self):\n        return super().__repr__()[:1]\n"
        "    @property\n    def p(self):\n        return 'new'\n"
        "    @classmethod\n    def make(cls):\n        return cls()\n",
        ns,
    )
    assert result.rebound == [] and ns["C"] is C
    assert (c.m(), c.kind, c.p, bound()) == ("new", "new", "new", "new")
    assert d.m() == "D new" and isinstance(d, ns["C"])
    assert not hasattr(c, "gone")
    assert c.added() == "<"  # zero-argument super() in a method that is new
    assert type(C.make()) is C


def test_a_subclass_whose_base_was_redefined_alongside_it_is_still_patched() -> None:
    ns = module()
    text = "class Base:\n    def who(self):\n        return 'base%d'\nclass Child(Base):\n    def who(self):\n        return 'child of ' + super().who()\n"
    run(text % 1, ns)
    Base, Child, child = ns["Base"], ns["Child"], ns["Child"]()
    result = run(text % 2, ns)
    assert result.rebound == []
    assert ns["Base"] is Base and ns["Child"] is Child and child.who() == "child of base2"


def test_class_changes_that_cannot_be_applied_are_reported() -> None:
    ns = module()
    run("class C:\n    pass\nclass S:\n    __slots__ = ('a',)", ns)
    C, c = ns["C"], ns["C"]()
    result = run("class C(dict):\n    pass\nclass S:\n    __slots__ = ('a', 'b')", ns)
    assert dict(result.rebound) == {"C": "its base classes changed", "S": "its __slots__ changed"}
    assert ns["C"] is not C and not isinstance(c, ns["C"])


def test_every_generation_someone_might_hold_is_patched() -> None:
    ns = module()
    ns["registry"] = []
    text = "def reg(fn):\n    registry.append(fn)\n    return fn\n@reg\ndef f():\n    return %d"
    for n in (1, 2, 3):
        run(text % n, ns)
    assert [fn() for fn in ns["registry"]] == [3, 3, 3] and ns["f"] is ns["registry"][0]


def test_tracebacks_and_inspect_see_the_evaluated_text() -> None:
    ns = module()
    whole = "# one\n# two\ndef boom():\n    return 1 / 0\n"
    run(
        "def boom():\n    return 1 / 0\n",
        ns,
        filename="<neomacs:t>",
        first_line=3,
        source_text=whole,
    )
    with pytest.raises(ZeroDivisionError) as info:
        ns["boom"]()
    frame = traceback.extract_tb(info.value.__traceback__)[-1]
    assert (frame.filename, frame.lineno, frame.line) == ("<neomacs:t>", 4, "return 1 / 0")
    assert inspect.getsource(ns["boom"]) == "def boom():\n    return 1 / 0\n"
    assert inspect.getsourcelines(ns["boom"])[1] == 3


def test_a_syntax_error_names_the_line_in_the_buffer() -> None:
    with pytest.raises(SyntaxError) as info:
        run("x = (", module(), filename="<neomacs:t>", first_line=1)
    assert info.value.filename == "<neomacs:t>"


def test_finding_what_to_evaluate() -> None:
    text = "import os\n\n@deco\ndef f(a):\n    return a\n\nclass C:\n    def m(self):\n        pass\n\nx = [1,\n     2]\n"
    assert source.definition_at(text, 5) == (3, 5)
    assert source.definition_at(text, 3) == (3, 5)
    assert source.definition_at(text, 9) == (7, 9)  # inside a class: the whole class
    assert source.definition_at(text, 12) == (11, 12)
    assert source.definition_at(text, 2) is None
    # A buffer that does not parse elsewhere still finds the statement.
    broken = "def ok():\n    return 1\n\ndef bad(:\n    pass\n"
    assert source.definition_at(broken, 2) == (1, 2)
    line = "y = foo(1)[0].bar + 2\n"
    assert line[slice(*source.expression_before(line, 13) or (0, 0))] == "foo(1)[0]"
    assert line[slice(*source.expression_before(line, 21) or (0, 0))] == "foo(1)[0].bar + 2"
    assert line[slice(*source.expression_before(line, 18) or (0, 0))] == "foo(1)[0].bar"
    assert source.expression_before("x = é.y + 1\n", 7) == (4, 7)  # positions are characters
    assert source.expression_before("if x:\n", 5) is None
    assert source.expression_before("def bad(:\n1 + 2", 15) == (10, 15)


def test_module_name_for_a_file() -> None:
    import neomacs.std.movement as movement

    assert source.module_name_for(Path(movement.__file__)) == "neomacs.std.movement"
    assert source.module_name_for(Path("/tmp/nowhere/lonely.py")) is None


# -- in a running editor ---------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Path]:
    with running_engine() as path:
        yield path


@pytest.fixture
def ui(engine: Path) -> Iterator[HeadlessUI]:
    with HeadlessUI(engine, rows=16, cols=70) as session:
        session.eval(
            "_s = ed.buffers.get('*scratch*'); _s.replace(0, len(_s), '')\n"
            f"_w = ed.frames[{session.frame_id}].window; _w.show(_s); _w.selections.set([(0, 0)])"
        )
        yield session
        session.client.notify("ui.detach")


def value(ui: HeadlessUI, expr: str) -> Any:
    return eval(ui.client.eval(expr))  # noqa: S307 - a repr from our own engine


def put(ui: HeadlessUI, text: str, name: str = "_s") -> None:
    ui.eval(
        f"{name}.replace(0, len({name}), {text!r}); _w.selections.set([(len({name}), len({name}))])"
    )


def test_redefine_a_builtin_command_and_rebind_a_key_from_scratch(ui: HeadlessUI) -> None:
    """The step's "done when"."""
    ui.type("ab")
    ui.keys("C-a", "C-f")
    assert ui.cursors() == [(1, 1)]
    put(
        ui,
        "@command\ndef forward_char(sel: Selection, n: Annotated[int, Prefix()] = 1) -> None:\n"
        "    'Move forward N characters, twice.'\n    sel.forward_char(2 * n)\n\n"
        "from typing import Annotated\nglobal_map.bind('C-f', 'forward_word')\nglobal_map.bind('<f5>', 'forward_char')\n",
    )
    ui.call("eval_buffer")
    assert ui.message == "Evaluated"
    put(ui, "abcdef ghi")
    ui.keys("C-a", "<f5>")  # the very next keystrokes
    assert ui.cursors() == [(1, 2)]
    ui.keys("<right>")  # another key bound to the same command name
    assert ui.cursors() == [(1, 4)]
    ui.keys("C-f")
    assert ui.cursors() == [(1, 6)]
    # Jump to the source of the command just defined: it is in the scratch buffer.
    ui.call("find_source", "forward_char")
    assert value(ui, "_w.buffer.name") == "*scratch*"
    # And of an untouched built-in: its file.
    ui.call("find_source", "backward_char")
    assert value(ui, "(_w.buffer.name, _w.buffer.line_text(_w.buffer.line_of(_w.cursor.pos)))") == (
        "movement.py",
        "@command",
    )
    # Put things back for the tests that follow, by evaluating the real file.
    ui.call("find_source", "backward_word")
    ui.call("eval_buffer")
    ui.eval("import neomacs.std; _i = ed.buffers.visit(neomacs.std.__file__); _w.show(_i)")
    ui.call("eval_buffer")
    assert value(ui, "ed.eval_warnings") == []
    ui.eval("_w.show(_s)")
    put(ui, "abcdef ghi")
    ui.keys("C-a", "C-f")
    assert ui.cursors() == [(1, 1)]
    assert value(ui, "global_map.lookup(['f5'])") is None  # the scratch binding is still there
    ui.eval("global_map.unbind('<f5>')")


def test_eval_last_expression_definition_and_selection(ui: HeadlessUI) -> None:
    put(ui, "def twice(x):\n    return x * 2\n\nresult = twice(4) + 1")
    ui.keys("C-x", "C-e")
    assert "NameError" in ui.message  # nothing has been evaluated yet
    ui.eval("_w.selections.set([(5, 5)])")
    ui.keys("C-M-x")  # the definition around the cursor
    assert ui.message == "Evaluated"
    ui.eval("_w.selections.set([(len(_s), len(_s))])")
    ui.keys("C-x", "C-e")
    assert ui.message == "9"
    ui.eval("_w.selections.set([(len(_s) - 4, len(_s) - 4)])")  # just after twice(4)
    ui.keys("C-x", "C-e")
    assert ui.message == "8"
    ui.eval(
        "_at = _s.text.index('twice(4)'); _w.selections.set([(_at + 11, _at + 12), (_at, _at + 8)])"
    )
    ui.call("eval_selection")
    assert ui.message == "1" and value(ui, "ed.messages[-2:]") == ["8", "1"]  # in buffer order
    ui.eval("_w.selections.set([(0, 0)])")
    ui.call("eval_selection")
    assert ui.message == "Nothing is selected"


def test_eval_expression_prompt_with_await(ui: HeadlessUI) -> None:
    ui.keys("M-:")
    assert ui.prompt == "Eval: "
    ui.type("await ed.minibuffer.read('Name? ')")
    ui.keys("<return>")
    assert ui.prompt == "Name? "
    ui.type("Ada")
    ui.keys("<return>")
    assert ui.prompt is None and ui.message == "'Ada'"


def test_a_long_value_is_truncated_in_the_message_and_whole_in_the_log(ui: HeadlessUI) -> None:
    put(ui, "list(range(200))")
    ui.keys("C-x", "C-e")
    assert ui.message.endswith("…") and len(ui.message) < 260
    assert value(ui, "ed.messages[-2]") == repr(list(range(200)))


def test_traceback_buffer_jumps_to_unsaved_code(ui: HeadlessUI) -> None:
    put(
        ui,
        "# a comment\ndef inner():\n    return {}['missing']\n\ndef outer():\n    return inner()\n\nouter()",
    )
    ui.call("eval_buffer")
    assert ui.message.startswith("KeyError: 'missing'")
    assert value(ui, "_w.buffer.name") == "*scratch*"  # the window is not taken over
    ui.call("show_traceback")
    lines = value(ui, "_w.buffer.text").split("\n")
    assert lines[:7] == [
        "Traceback (most recent call last):",
        "  <neomacs:*scratch*>, line 8, in <module>",
        "    outer()",
        "  <neomacs:*scratch*>, line 6, in outer",
        "    return inner()",
        "  <neomacs:*scratch*>, line 3, in inner",
        "    return {}['missing']",
    ]
    assert lines[7] == "KeyError: 'missing'"
    assert value(ui, "len(_w.buffer.layer('links'))") == 3
    with pytest.raises(Exception, match="read-only|ReadOnly"):
        ui.eval("_w.buffer.insert(0, 'x')")
    ui.eval("_w.selections.set([(_w.buffer.line_start(6) + 4, _w.buffer.line_start(6) + 4)])")
    ui.keys("<return>")
    assert value(ui, "(_w.buffer.name, _w.buffer.line_of(_w.cursor.pos))") == ("*scratch*", 3)
    ui.keys("M-,")  # and back to the traceback
    assert value(ui, "_w.buffer.name") == "*traceback*"
    ui.keys("q")
    assert value(ui, "_w.buffer.name") == "*scratch*"


def test_a_syntax_error_is_reported_with_its_line(ui: HeadlessUI) -> None:
    put(ui, "x = 1\ny = (\n")
    ui.call("eval_buffer")
    assert ui.message.startswith("SyntaxError")
    assert "<neomacs:*scratch*>, line 2" in value(ui, "ed.buffers.get('*traceback*').text")


def test_a_fallback_to_rebinding_warns(ui: HeadlessUI) -> None:
    put(ui, "class Shape:\n    pass\n_shape = Shape()")
    ui.call("eval_buffer")
    put(ui, "class Shape(dict):\n    pass")
    ui.call("eval_buffer")
    assert ("Shape was replaced, not updated in place: its base classes changed", "warning") in [
        (text.split(". ")[0].replace("neomacs_scratch.", ""), kind)
        for text, kind in ui.model.messages
    ]
    assert value(ui, "ed.eval_warnings[-1]") == (
        "neomacs_scratch",
        "Shape",
        "its base classes changed",
    )
    ui.eval("ed.eval_warnings.clear()")


def test_keep_and_forget(ui: HeadlessUI) -> None:
    put(
        ui,
        "history = keep('test.history', list)\nhistory.append(len(history))\nplain = []\nplain.append(1)\n(history, plain)",
    )
    for expected in ("([0], [1])", "([0, 1], [1])", "([0, 1, 2], [1])"):
        ui.call("eval_buffer")
        assert ui.message == expected
    ui.eval("forget('test.history')")
    ui.call("eval_buffer")
    assert ui.message == "([0], [1])"


def test_evaluating_a_file_in_a_package_goes_into_its_module_and_drops_stale_registrations(
    ui: HeadlessUI, tmp_path: Path
) -> None:
    package = tmp_path / "livepkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    first = (
        "from neomacs.commands import command\nfrom neomacs.keymap import global_map\nfrom neomacs.editor import ed\n"
        "@command(name='livepkg.one')\ndef one():\n    ed.message('one')\n"
        "@command(name='livepkg.two')\ndef two():\n    ed.message('two')\n"
        "global_map.bind('<f7>', 'livepkg.one')\nglobal_map.bind('<f8>', 'livepkg.two')\nSTATE = 1\n"
    )
    (package / "tools.py").write_text(first)
    ui.eval(f"import sys; sys.path.insert(0, {str(tmp_path)!r})")
    ui.open(package / "tools.py")
    ui.eval("_t = _w.buffer")
    ui.call("eval_buffer")  # the module was never imported: it is created
    assert value(ui, "(sys.modules['livepkg.tools'].STATE, ed.commands['livepkg.two'].owner)") == (
        1,
        "livepkg.tools",
    )
    ui.keys("<f8>")
    assert ui.message == "two"
    # Remove one command and its key, change the other, and evaluate again.
    second = first.replace("@command(name='livepkg.two')\ndef two():\n    ed.message('two')\n", "")
    second = second.replace("global_map.bind('<f8>', 'livepkg.two')\n", "").replace(
        "'one'", "'uno'"
    )
    put(ui, second, "_t")
    ui.call("eval_buffer")
    assert value(ui, "'livepkg.two' in ed.commands") is False
    assert value(ui, "global_map.lookup(['f8'])") is None
    ui.keys("<f7>")
    assert ui.message == "uno"
    assert any("Removed command 'livepkg.two'" in m for m in value(ui, "ed.messages[-8:]"))
    # A partial evaluation removes nothing.
    ui.eval("_w.selections.set([(0, len(_t.line_text(1)))])")
    ui.call("eval_selection")
    assert value(ui, "'livepkg.one' in ed.commands") is True
    # Loading the file from disk is the same pipeline.
    (package / "tools.py").write_text(first)
    ui.eval(
        f"from neomacs.live import run as _run\nawait _run.load_file({str(package / 'tools.py')!r})"
    )
    ui.keys("<f8>")
    assert ui.message == "two"
    ui.eval(
        "ed.buffers.kill(_t); global_map.unbind('<f7>'); global_map.unbind('<f8>'); _w.show(_s)"
    )


def test_repl(ui: HeadlessUI) -> None:
    ui.call("repl")
    assert value(ui, "_w.buffer.name") == "*repl*"
    ui.type("1 + 1")
    ui.keys("<return>")
    ui.type("def sq(x):")
    ui.keys("<return>")
    ui.type("    return x * x")
    ui.keys("<return>", "<return>")
    ui.type("sq(7)")
    ui.keys("<return>")
    ui.type("1 / 0")
    ui.keys("<return>")
    ui.type("await asyncio.sleep(0, 'slept')")
    ui.keys("<return>")
    text = value(ui, "_w.buffer.text")
    assert text == (
        ">>> 1 + 1\n2\n>>> def sq(x):\n    return x * x\n>>> sq(7)\n49\n"
        ">>> 1 / 0\nZeroDivisionError: division by zero\n>>> await asyncio.sleep(0, 'slept')\n'slept'\n>>> "
    )
    ui.eval("_w.show(_s)")


def test_describe_command_key_option_and_apropos(ui: HeadlessUI) -> None:
    ui.call("describe_command", "kill_line")
    text = value(ui, "_w.buffer.text")
    assert value(ui, "_w.buffer.name") == "*help*"
    assert text.startswith("kill_line\n\nkill_line(") and "Keys: C-k" in text
    assert "Defined in neomacs.std.editing, " in text and "editing.py, line " in text
    ui.eval("_h = _w.buffer; _w.selections.set([(len(_h) - 3, len(_h) - 3)])")
    ui.keys("<return>")  # the link to the definition
    assert value(ui, "_w.buffer.name") == "editing.py"
    assert "def kill_line" in value(
        ui,
        "'\\n'.join(_w.buffer.line_text(_w.buffer.line_of(_w.cursor.pos) + n) for n in range(3))",
    )
    ui.eval("_w.show(_s)")

    ui.keys("C-h", "k")
    ui.keys("C-x")
    ui.keys("C-f")
    assert value(ui, "_w.buffer.text").startswith("C-x C-f runs the command files.find\n")
    ui.keys("q")
    assert value(ui, "_w.buffer.name") == "*scratch*"
    ui.keys("C-h", "k", "<f12>")
    assert ui.message == "<f12> is undefined"

    ui.eval("Option('test_width', int, 4, 'How wide a test is.')")
    ui.keys("C-h", "v")
    ui.type("test_w")
    assert ui.candidates() == ["test_width"]
    ui.keys("<tab>", "<return>")
    text = value(ui, "_w.buffer.text")
    assert "How wide a test is." in text and "Value here: 4" in text and "Default: 4" in text
    ui.keys("q")
    ui.eval("_s.options.test_width = 9")
    ui.keys("C-h", "v")
    ui.type("test_width")
    ui.keys("<return>")
    assert "Value here: 9  (from buffer)" in value(ui, "_w.buffer.text")
    ui.keys("q")

    ui.call("apropos", "^kill_|test_width")
    lines = value(ui, "_w.buffer.text").split("\n")
    assert lines[0] == "Commands" and "kill_line  (C-k)  Kill" in " ".join(lines)
    assert "Options" in lines and any(line.startswith("test_width  How wide") for line in lines)
    ui.keys("q")
    ui.call("apropos", "zzzqqq")
    assert ui.message == "Nothing matches zzzqqq"


def test_engine_eval_with_await_and_ordering(ui: HeadlessUI) -> None:
    assert ui.client.eval("await asyncio.sleep(0.01, 'later')") == "'later'"
    # Code that does not await has run by the time the next message is handled.
    ui.client.notify("engine.eval", "_order = [1]")
    ui.client.notify("engine.eval", "_order.append(2)")
    assert ui.client.eval("_order") == "[1, 2]"


def test_every_standard_module_survives_eval_buffer(ui: HeadlessUI) -> None:
    """Gate D by the front door: the real command, on every module, twice."""
    from neomacs.tools.reevaluate import module_files

    before = value(ui, "(len(ed.commands), sorted(ed.commands), len(list(global_map.bindings())))")
    ui.eval(
        "import neomacs.std.editing as _ed_mod; _held = _ed_mod.kill_line; ed.eval_warnings.clear()"
    )
    for _ in range(2):
        for path in module_files():
            ui.open(path)
            ui.call("eval_buffer")
            assert ui.message == "Evaluated", (path, ui.message)
    assert value(ui, "ed.eval_warnings") == []
    assert (
        value(ui, "(len(ed.commands), sorted(ed.commands), len(list(global_map.bindings())))")
        == before
    )
    assert value(
        ui, "_ed_mod.kill_line is _held and _held.__code__ is ed.commands['kill_line'].fn.__code__"
    )
    assert value(ui, "_held.__code__.co_filename == _ed_mod.__file__")
    assert [item["label"] for item in ui.model.menu] == ["File", "Edit", "Tools", "Help"]
    # The editor still edits: typing, several cursors, kill and yank, undo, a prompt.
    ui.eval("_w.show(_s)")
    put(ui, "")
    ui.type("one two")
    ui.keys("<return>")
    ui.type("three four")
    ui.keys("C-a", "C-M-p", "M-d", "C-e", "C-y")
    assert value(ui, "_s.text") == " twoone\n fourthree"
    ui.keys("C-g", "C-/")
    ui.keys("M-x")
    ui.type("goto_l")
    ui.keys("<return>")
    ui.type("1")
    ui.keys("<return>")
    assert ui.cursors() == [(1, 0)]
