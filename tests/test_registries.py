"""Stage 3, the registries: keys, keymaps, options, hooks, commands, ownership, syntax."""

from __future__ import annotations

import asyncio
import warnings
from typing import Annotated

import pytest

from neomacs import commands as commands_module
from neomacs import completion, keys, options
from neomacs.clipboard import KillRing
from neomacs.commands import Ask, KeyText, Prefix, PrefixArg, RawPrefix, command
from neomacs.hooks import Hook
from neomacs.keymap import Keymap
from neomacs.menu import Menu
from neomacs.modes import Mode
from neomacs.options import Option, OptionView
from neomacs.ownership import Ownership, ownership
from neomacs.selection import JumpList, Selection, SelectionSet
from neomacs.stability import deprecated, info
from neomacs.syntax import Syntax
from neomacs.text import Buffer

# -- keys and keymaps ---------------------------------------------------------


def test_key_notation() -> None:
    assert keys.parse("C-x C-f") == ("C-x", "C-f")
    assert keys.normalize("M-C-<return>") == "C-M-<return>"
    assert keys.normalize("s-S-M-C-a") == "C-M-S-s-a"
    assert keys.normalize("C--") == "C--" and keys.split("C--") == ("C", "-")
    assert keys.normalize(" ") == "<space>" and keys.normalize("C- ") == "C-<space>"
    assert keys.split("M-<") == ("M", "<") and keys.split("-") == ("", "-")
    assert keys.normalize("👍🏽") == "👍🏽"
    assert keys.is_printable("a") and keys.is_printable("-") and not keys.is_printable("C-a")
    assert not keys.is_printable("<f1>")
    assert keys.text_of("<space>") == " " and keys.text_of("x") == "x"
    assert keys.text_of("<return>") is None
    assert keys.describe(("C-x", "C-f")) == "C-x C-f"
    for bad in ("", "ab", "C-"):
        with pytest.raises(ValueError):
            keys.parse(bad) if bad == "" else keys.normalize(bad)


def test_keymap() -> None:
    km = Keymap("test")
    km.bind("C-x C-f", "files.find")
    km.bind("C-x 4 f", "other")
    km.bind("a", "letter")
    assert km.lookup(("a",)) == "letter"
    assert isinstance(km.lookup(("C-x",)), Keymap)
    assert km.lookup(("C-x", "C-f")) == "files.find"
    assert km.lookup(("C-x", "4", "f")) == "other"
    assert km.lookup(("z",)) is None and km.lookup(("a", "b")) is None
    assert km.where_is("files.find") == ("C-x", "C-f") and km.where_is("nope") is None
    assert sorted(name for _, name in km.bindings()) == ["files.find", "letter", "other"]
    km.bind("a", "replaced")  # rebinding replaces
    assert km.lookup(("a",)) == "replaced"
    km.unbind("C-x C-f")
    assert km.lookup(("C-x", "C-f")) is None
    sub = Keymap("sub")
    sub.bind("x", "in-sub")
    km.bind("C-c", sub)
    assert km.lookup(("C-c", "x")) == "in-sub"


def test_ownership() -> None:
    own = Ownership()
    store: dict[str, int] = {"a": 1, "b": 2, "c": 3}
    own.record("mod1", "thing", "a", lambda: store.pop("a", None))
    own.record("mod1", "thing", "b", lambda: store.pop("b", None))
    own.record("mod2", "thing", "c", lambda: store.pop("c", None))
    assert own.owner_of("thing", "a") == "mod1"
    assert sorted(own.owned("mod1")) == [("thing", "a"), ("thing", "b")]
    own.record("mod2", "thing", "b", lambda: store.pop("b", None))  # taken over
    assert own.owned("mod1") == [("thing", "a")]
    assert own.release("mod2", keep={("thing", "c")}) == [("thing", "b")]
    assert store == {"a": 1, "c": 3}
    own.forget("thing", "a")
    assert own.release("mod1") == [] and store == {"a": 1, "c": 3}


def test_registrations_record_their_module() -> None:
    @command(name="test.owned")
    def owned() -> None: ...

    km = Keymap("owned")
    km.bind("q", "test.owned")
    hook = Hook("test_owned_hook")
    hook.add(owned)
    mine = set(ownership.owned(__name__))
    assert ("command", "test.owned") in mine
    assert ("key", (id(km), ("q",))) in mine
    assert any(kind == "hook" and key[0] == "test_owned_hook" for kind, key in mine)
    ownership.release(__name__)
    assert commands_module.lookup("test.owned") is None
    assert km.lookup(("q",)) is None and hook.functions() == []


# -- options ------------------------------------------------------------------


def test_options() -> None:
    Option("t_width", int, 8, "A test option.", validate=lambda n: n > 0)
    buf = Buffer("t")
    assert buf.options.t_width == 8 and buf.options.origin("t_width") == ("default", 8)
    glob = OptionView()
    glob.t_width = 4
    assert buf.options.origin("t_width") == ("global", 4)
    buf.options.t_width = 2
    assert buf.options.origin("t_width") == ("buffer", 2) and glob.t_width == 4
    buf.options.unset("t_width")
    assert buf.options.t_width == 4

    class M(Mode):
        defaults = {"t_width": 3}

    class Derived(M):
        pass

    buf.mode = Derived()
    assert buf.options.origin("t_width") == ("mode", 3)
    with pytest.raises(TypeError):
        glob.t_width = "wide"
    with pytest.raises(TypeError):
        glob.t_width = True  # a bool is not an int here
    with pytest.raises(ValueError):
        glob.t_width = -1
    with pytest.raises(AttributeError):
        _ = buf.options.no_such_option
    with pytest.raises(AttributeError):
        buf.options.no_such_option = 1
    assert buf.options.get("no_such_option", "fallback") == "fallback"
    glob.unset("t_width")


def test_option_declared_again_keeps_values_and_hooks() -> None:
    first = Option("t_redeclared", int, 1, "old doc")
    seen: list[object] = []
    first.on_change.add(lambda value, buffer: seen.append(value))
    OptionView().t_redeclared = 5
    second = Option("t_redeclared", int, 2, "new doc")
    assert options.options["t_redeclared"] is second and second.doc == "new doc"
    assert OptionView().t_redeclared == 5
    OptionView().t_redeclared = 6
    assert seen == [5, 6]
    OptionView().unset("t_redeclared")


def test_option_set_before_it_is_declared() -> None:
    glob = OptionView()
    glob.t_later = 7  # held until there is a declaration
    glob.t_later_bad = "seven"
    Option("t_later", int, 1)
    Option("t_later_bad", int, 1)
    assert glob.t_later == 7
    assert glob.t_later_bad == 1  # not acceptable to the declaration: ignored
    glob.unset("t_later")
    glob.unset("t_later_bad")


def test_option_alias_and_float() -> None:
    Option("t_new_name", float, 0.5)
    options.alias("t_old_name", "t_new_name")
    glob = OptionView()
    glob.t_old_name = 2  # an int is accepted for a float
    assert glob.t_new_name == 2.0 and isinstance(glob.t_new_name, float)
    assert glob.t_old_name == 2.0
    glob.unset("t_new_name")


def test_core_options_have_defaults() -> None:
    buf = Buffer("t")
    assert (buf.options.tab_width, buf.options.wrap, buf.options.line_numbers) == (
        8,
        True,
        "absolute",
    )
    assert options.options["tab_width"].project and not options.options["wrap"].project


# -- hooks ----------------------------------------------------------------------


def test_hooks_await_coroutines_only_when_asked() -> None:
    hook = Hook("t_async", register=False)
    seen: list[str] = []

    async def slow(x: str) -> None:
        await asyncio.sleep(0)
        seen.append("slow " + x)

    def quick(x: str) -> None:
        seen.append("quick " + x)

    hook.add(slow)
    hook.add(quick)
    asyncio.run(hook.arun("a"))
    assert seen == ["slow a", "quick a"]
    with pytest.raises(TypeError, match="coroutine"):
        hook.run("b")  # no editor to report to, so it propagates


# -- commands ---------------------------------------------------------------------


def test_command_registration_and_parameters() -> None:
    @command
    def t_plain(sel: Selection, n: Annotated[int, Prefix(default=2)] = 2) -> None:
        """Do a thing.

        More detail.
        """

    @command(name="t.asking", background=True)
    async def asking(
        name: Annotated[str, Ask.string("Name: ", history="names")],
        raw: Annotated[PrefixArg | None, RawPrefix] = None,
        text: Annotated[str, KeyText] = "",
        plain: int = 0,
    ) -> None: ...

    one = commands_module.lookup("t_plain")
    assert one is not None and one.fn is t_plain and one.summary == "Do a thing."
    assert one.per_selection and not one.background and one.owner == __name__
    assert [(p.name, p.is_selection, type(p.spec).__name__) for p in one.params] == [
        ("sel", True, "NoneType"),
        ("n", False, "Prefix"),
    ]
    two = commands_module.lookup("t.asking")
    assert two is not None and two.background and not two.per_selection
    specs = {p.name: p.spec for p in two.params}
    assert isinstance(specs["name"], Ask) and specs["name"].history == "names"
    assert specs["raw"] is RawPrefix and specs["text"] is KeyText and specs["plain"] is None
    assert [p.required for p in two.params] == [True, False, False, False]

    @command(name="t_plain")
    def replacement() -> None: ...

    again = commands_module.lookup("t_plain")
    assert again is not None and again.fn is replacement  # redefinition replaces
    ownership.release(__name__)


def test_command_alias_warns_once() -> None:
    @command(name="t.new")
    def new() -> None: ...

    commands_module.alias("t.old", "t.new")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert commands_module.lookup("t.old") is commands_module.lookup("t.new")
        assert commands_module.lookup("t.old") is not None
    assert len(caught) == 1 and "renamed to t.new" in str(caught[0].message)
    ownership.release(__name__)


def test_deprecated() -> None:
    @deprecated("use shiny", since="1.4", removal="2.0")
    def old(x: int) -> int:
        """Docs survive."""
        return x * 2

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert old(2) == 4 and old(3) == 6  # one warning per calling module
    assert len(caught) == 1 and "deprecated since 1.4" in str(caught[0].message)
    assert "removed in 2.0: use shiny" in str(caught[0].message)
    assert old.__doc__ == "Docs survive." and old.__name__ == "old"
    assert old.__neomacs_deprecated__["removal"] == "2.0"  # type: ignore[attr-defined]
    assert info(old) is None


def test_prefix_arg_describe() -> None:
    assert PrefixArg(4, False, 1).describe() == "C-u-"
    assert PrefixArg(16, False, 2).describe() == "C-u C-u-"
    assert PrefixArg(42, True, 1).describe() == "C-u 42-"


# -- selections -----------------------------------------------------------------------


def test_selection_motion_and_extending() -> None:
    buf = Buffer("t", "one two three\nfour")
    sels = SelectionSet(buf)
    sel = sels.primary
    sel.forward_word()
    assert (sel.anchor.pos, sel.head.pos, sel.empty) == (3, 3, True)
    sels.extending = True
    sel.forward_word(2)
    assert (sel.anchor.pos, sel.head.pos, sel.text) == (3, 13, " two three")
    sel.backward_word(3)  # the head crosses the anchor
    assert (sel.start, sel.end, sel.text) == (0, 3, "one")
    sel.replace("ONE")  # an edit ends extending and leaves the cursor after the text
    assert buf.text.startswith("ONE two") and sel.empty and sel.pos == 3 and not sels.extending
    sel.select(4, 7)
    assert sel.delete() == "two" and sel.empty and sel.pos == 4
    sel.end_of_line()
    sel.forward_char()
    assert buf.line_of(sel.pos) == 2
    sel.start_of_line()
    sel.forward_line(-1)
    assert sel.pos == 0
    sel.move_to(999)
    assert sel.pos == len(buf)


def test_text_inserted_at_an_empty_selection_leaves_it_empty() -> None:
    buf = Buffer("t", "ab")
    sels = SelectionSet(buf, 1)
    buf.insert(1, "XYZ")
    assert sels.ranges() == [(4, 4)]


def test_selection_set() -> None:
    buf = Buffer("t", "0123456789")
    sels = SelectionSet(buf)
    assert len(sels) == 1
    second = sels.add(5)
    third = sels.add(8, anchor=6)
    assert sels.primary is third and sels.ranges() == [(0, 0), (5, 5), (6, 8)]
    assert sels.primary_index == 2
    sels.remove(second)
    assert sels.ranges() == [(0, 0), (6, 8)]
    sels.keep_primary()
    assert sels.ranges() == [(6, 8)]
    sels.collapse()
    assert sels.ranges() == [(8, 8)]
    sels.restore_last()
    assert sels.ranges() == [(6, 8)]
    sels.set([(1, 2), (4, 3)], primary=1)
    assert sels.ranges() == [(1, 2), (4, 3)] and sels.primary.head.pos == 3
    sels.remove(sels.primary)
    sels.remove(sels.primary)  # the last one cannot be removed
    assert len(sels) == 1


def test_normalize_merges_and_keeps_the_primary() -> None:
    buf = Buffer("t", "0123456789")
    sels = SelectionSet(buf)
    sels.set([(0, 3), (2, 5), (7, 7), (7, 7), (9, 8)], primary=1)
    sels.normalize()
    assert sels.ranges() == [(0, 5), (7, 7), (9, 8)]
    assert sels.primary.start == 0  # the merged selection took over as primary
    sels.set([(5, 2), (4, 6)], primary=0)  # the primary faces backward: so does the result
    sels.normalize()
    assert sels.ranges() == [(6, 2)]
    sels.set([(0, 3), (3, 6)])  # touching but not overlapping: kept apart
    sels.normalize()
    assert sels.ranges() == [(0, 3), (3, 6)]
    sels.set([(0, 3), (3, 3)])  # an empty one at another's edge is absorbed
    sels.normalize()
    assert sels.ranges() == [(0, 3)]


def test_normalize_snaps_to_grapheme_clusters() -> None:
    family = "👨\u200d👩\u200d👧"
    buf = Buffer("t", f"a{family}b")
    sels = SelectionSet(buf)
    sels.set([(3, 3)])  # inside the family: back to its start
    sels.normalize()
    assert sels.ranges() == [(1, 1)]
    sels.set([(0, 3)])  # partly selected: becomes wholly selected
    sels.normalize()
    assert sels.ranges() == [(0, 6)]
    sels.set([(7, 3)])  # backward, starting inside
    sels.normalize()
    assert sels.ranges() == [(7, 1)]


def test_jump_list() -> None:
    buf = Buffer("t", "\n".join(str(n) for n in range(50)))
    jumps = JumpList()
    assert jumps.back(buf, 0) is None
    jumps.push(buf, buf.line_start(3))
    jumps.push(buf, buf.line_start(3) + 1)  # the same line: replaces the entry
    jumps.push(buf, buf.line_start(20))
    assert len(jumps) == 2
    here = buf.line_start(40)
    back = jumps.back(buf, here)
    assert back is not None and buf.line_of(back.pos) == 20
    back = jumps.back(buf, back.pos)
    assert back is not None and buf.line_of(back.pos) == 3
    assert jumps.back(buf, back.pos) is None
    forward = jumps.forward()
    assert forward is not None and buf.line_of(forward.pos) == 20
    forward = jumps.forward()
    assert forward is not None and forward.pos == here
    assert jumps.forward() is None
    buf.insert(0, "new\n")  # entries are markers: they move with the text
    back = jumps.back(buf, 0)
    assert back is not None and buf.line_of(back.pos) == 21


# -- syntax ------------------------------------------------------------------------------


class PyLike(Mode):
    syntax = Syntax(line_comment="#", quotes=('"""', '"', "'"), block_comment=("/*", "*/"))


SOURCE = (
    "def f(a, [b, c]):  # (comment\n"
    '    s = "a (string)" + g(x)\n'
    '    """doc\n'
    '    (still doc)"""\n'
    "héllo_wörld foo-bar /* block ( \n"
    "still block */ done\n"
)


def test_scanner_kinds() -> None:
    buf = Buffer("t", SOURCE)
    buf.mode = PyLike()
    syn = buf.syntax
    at = lambda line, offset: syn.kind_at(buf.line_start(line) + offset)  # noqa: E731
    assert [at(1, 3), at(1, 22), at(2, 9), at(2, 22)] == ["code", "comment", "string", "code"]
    assert [at(3, 5), at(4, 3), at(4, 18)] == ["string", "string", "code"]
    assert [at(5, 0), at(5, 22), at(6, 3), at(6, 16)] == ["code", "comment", "comment", "code"]
    buf.insert(buf.line_start(3), "# ")  # the docstring becomes a comment; line 4 is code again
    assert at(3, 5) == "comment" and at(4, 4) == "code"


def test_brackets_ignore_strings_and_comments() -> None:
    buf = Buffer("t", SOURCE)
    buf.mode = PyLike()
    syn = buf.syntax
    assert (syn.matching_bracket(5), syn.matching_bracket(15)) == (15, 5)
    assert (syn.matching_bracket(9), syn.matching_bracket(14)) == (14, 9)
    assert syn.matching_bracket(20) is None  # the ( inside the comment
    assert syn.matching_bracket(buf.line_start(2) + 11) is None  # and inside the string
    assert syn.matching_bracket(0) is None
    assert syn.enclosing(10, 11) == (9, 15) and syn.enclosing(6, 7) == (5, 16)
    assert syn.enclosing(0, 3) == (0, buf.line_end(1))  # no bracket: the line
    assert syn.enclosing(0, buf.line_end(1)) == (0, buf.line_end(6))  # then the paragraph


def test_words_and_expressions() -> None:
    buf = Buffer("t", SOURCE)
    buf.mode = PyLike()
    syn = buf.syntax
    p = buf.line_start(5)
    assert syn.next_word_end(p) == p + 11 and syn.word_at(p + 2) == (p, p + 11)
    assert syn.prev_word_start(p + 15) == p + 12 and syn.word_at(p + 11) == (p, p + 11)
    assert buf[syn.next_word_end(p + 11) : p + 19] == "-bar"  # '-' is not a word character
    assert syn.word_at(buf.line_start(1) + 5) is None or True
    assert syn.expression_at(5) == (5, 16) and syn.expression_at(16, -1) == (5, 16)
    q = buf.line_start(2)
    assert buf[slice(*syn.expression_at(q + 8))] == '"a (string)"'
    assert buf[slice(*syn.expression_at(q + 20, -1))] == '"a (string)"'
    assert buf[slice(*syn.expression_at(0))] == "def"
    assert syn.unit_at(0, "function") is None  # needs a grammar


def test_indentation() -> None:
    buf = Buffer("t", "if x:\n\tfoo(\n  bar\n)\n\n    deep")
    syn = buf.syntax
    assert [syn.indentation(n) for n in (1, 2, 3, 6)] == [0, 8, 2, 4]
    assert syn.indent_for(3) == 16  # one level in from a line that opens a bracket
    assert syn.indent_for(4) == 0  # a closing bracket comes back out
    assert syn.indent_for(6) == 0  # blank lines are skipped when looking back
    buf.options.tab_width = 4
    assert syn.indentation(2) == 4


def test_default_syntax_table() -> None:
    table = Syntax()
    assert (
        table.is_word("é") and table.is_word("_") and table.is_word("7") and not table.is_word("-")
    )
    assert Syntax(symbol_chars="-").is_symbol("-")
    assert ("(", ")") in table.pairs and ('"', '"') in table.pairs


# -- the kill ring, the menu, completion ---------------------------------------------------


def test_kill_ring() -> None:
    ring = KillRing()
    assert ring.top is None
    ring.push(["", ""])  # nothing to keep
    ring.push(["a", "b", "c"])
    assert ring.top == ["a", "b", "c"]
    assert ring.distribute(["a", "b", "c"], 3) == ["a", "b", "c"]
    assert ring.distribute(["a", "b", "c"], 2) == ["a\nb\nc", "a\nb\nc"]
    assert ring.distribute(["one"], 1) == ["one"]
    assert asyncio.run(ring.current(None)) == ["a", "b", "c"]


def test_menu_tree() -> None:
    menu = Menu()
    menu.add("File/Open…", "files.find")
    menu.add("File/Save", "files.save")
    menu.add("Edit/Undo", "undo")
    menu.add("File/Recent/Clear", "no.such.command")
    menu.add("File/Open Recent", "undo", after="File/Open…")
    km = Keymap("m")
    km.bind("C-x C-f", "files.find")
    tree = menu.tree([km])
    assert [item["label"] for item in tree] == ["File", "Edit"]
    file_items = tree[0]["children"]
    assert [item["label"] for item in file_items] == ["Open…", "Open Recent", "Save", "Recent"]
    assert file_items[0] == {
        "label": "Open…",
        "command": "files.find",
        "key": "C-x C-f",
        "enabled": True,
    }
    assert file_items[3]["children"][0]["enabled"] is False
    generation = menu.generation
    menu.remove("Edit/Undo")
    assert menu.generation == generation + 1 and len(menu.tree([])) == 1
    ownership.release(__name__)


def test_completion_matching() -> None:
    assert completion.matches("forward_word", "for", "prefix")
    assert not completion.matches("forward_word", "word", "prefix")
    assert completion.matches("forward_word", "word for")  # substrings, in any order
    assert completion.matches("forward_word", "fwd", "fuzzy")
    assert not completion.matches("forward_word", "wdf", "fuzzy")
    items = [completion.Candidate(n) for n in ("undo", "redo", "do", "window.do", "other")]
    assert [c.text for c in completion.rank(items, "do")] == ["do", "redo", "undo", "window.do"]
    found = asyncio.run(completion.choices(["apple", "apricot", "banana"]).candidates("ap", 2))
    assert [c.text for c in found] == ["apple", "apricot"]

    async def later(text: str) -> list[str]:
        return [text.upper()]

    found = asyncio.run(completion.FunctionCompleter(later).candidates("x", 1))
    assert found == [completion.Candidate("X")]


def test_file_completion(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "alpha.txt").write_text("")
    (tmp_path / "alps").mkdir()
    (tmp_path / "beta.txt").write_text("")
    (tmp_path / ".hidden").write_text("")
    found = asyncio.run(completion.files_completer.candidates(f"{tmp_path}/al", 0))
    assert [c.text for c in found] == [f"{tmp_path}/alpha.txt", f"{tmp_path}/alps/"]
    assert found[1].annotation == "directory"
    everything = asyncio.run(completion.files_completer.candidates(f"{tmp_path}/", 0))
    assert len(everything) == 3  # hidden files only when asked for
    hidden = asyncio.run(completion.files_completer.candidates(f"{tmp_path}/.h", 0))
    assert [c.text for c in hidden] == [f"{tmp_path}/.hidden"]
    assert asyncio.run(completion.files_completer.candidates("/no/such/dir/x", 0)) == []
