"""The standard commands, and the default key bindings and menu.

Everything here is written against the public API, as an extension would be.
"""

from neomacs.keymap import global_map
from neomacs.std import (
    cursors,
    editing,
    evaluation,
    files,
    help,  # noqa: A004
    mouse,
    movement,
    prefix,
    prompts,
)

__all__ = [
    "cursors",
    "editing",
    "evaluation",
    "files",
    "help",
    "mouse",
    "movement",
    "prefix",
    "prompts",
]

global_map.default = "self_insert"

_BINDINGS = {
    # movement
    "C-f": "forward_char", "<right>": "forward_char",
    "C-b": "backward_char", "<left>": "backward_char",
    "M-f": "forward_word", "C-<right>": "forward_word", "M-<right>": "forward_word",
    "M-b": "backward_word", "C-<left>": "backward_word", "M-<left>": "backward_word",
    "C-a": "start_of_line", "<home>": "start_of_line",
    "C-e": "end_of_line", "<end>": "end_of_line",
    "C-n": "next_line", "<down>": "next_line",
    "C-p": "previous_line", "<up>": "previous_line",
    "C-v": "page_down", "<next>": "page_down",
    "M-v": "page_up", "<prior>": "page_up",
    "M-<": "buffer_start", "C-<home>": "buffer_start",
    "M->": "buffer_end", "C-<end>": "buffer_end",
    "M-g g": "goto_line",
    "M-,": "jump_back", "C-M-,": "jump_forward",
    # editing
    "<return>": "newline", "<tab>": "self_insert_tab", "<space>": "self_insert",
    "<backspace>": "delete_backward_char", "<delete>": "delete_char", "C-d": "delete_char",
    "C-t": "transpose_chars",
    "C-k": "kill_line", "M-d": "kill_word", "M-<backspace>": "backward_kill_word",
    "C-w": "kill_selection", "M-w": "copy_selection", "C-y": "yank",
    "s-x": "kill_selection", "s-c": "copy_selection", "s-v": "yank", "s-z": "undo", "s-Z": "redo",
    "C-/": "undo", "C-_": "undo", "M-/": "redo", "C-?": "redo",
    "C-<space>": "set_anchor", "C-x h": "select_all", "s-a": "select_all",
    "C-x C-x": "restore_last_selection",
    "C-g": "keyboard_quit",
    # cursors
    "C-M-<down>": "add_cursor_below", "C-M-<up>": "add_cursor_above",
    "C-M-n": "add_cursor_below", "C-M-p": "add_cursor_above",
    # prefix argument
    "C-u": "universal_argument", "M--": "negative_argument",
    **{f"M-{d}": "digit_argument" for d in "0123456789"},
    # files, buffers, commands
    "C-x C-f": "files.find", "s-o": "files.find",
    "C-x C-s": "files.save", "s-s": "files.save",
    "C-x C-w": "files.save_as",
    "C-x b": "buffers.switch", "C-x k": "buffers.kill",
    "M-x": "execute_command",
    "C-x 5 2": "new_frame",
    "C-x C-c": "engine.exit",
    # evaluation
    "C-c C-t": "show_traceback", "C-c C-i": "debug.inspect",
    "C-x C-e": "eval_last_expression", "C-M-x": "eval_definition", "M-:": "eval_expression",
    "C-c C-b": "eval_buffer", "C-c C-r": "eval_selection",
    # help
    "C-h k": "describe_key", "C-h x": "describe_command", "C-h v": "describe_option",
    "C-h a": "apropos", "M-.": "find_source",
    "<f1> k": "describe_key", "<f1> x": "describe_command", "<f1> v": "describe_option",
    "<f1> a": "apropos",
    # mouse
    "<mouse-1>": "mouse.set_cursor", "<drag-mouse-1>": "mouse.drag",
    "M-<mouse-1>": "mouse.add_cursor",
    "<double-mouse-1>": "mouse.select_word", "<triple-mouse-1>": "mouse.select_line",
}  # fmt: skip

for _keys, _command in _BINDINGS.items():
    global_map.bind(_keys, _command)

MENU = [
    ("File/Open…", "files.find"),
    ("File/Save", "files.save"),
    ("File/Save As…", "files.save_as"),
    ("File/New Window", "new_frame"),
    ("Edit/Undo", "undo"),
    ("Edit/Redo", "redo"),
    ("Edit/Cut", "kill_selection"),
    ("Edit/Copy", "copy_selection"),
    ("Edit/Paste", "yank"),
    ("Edit/Select All", "select_all"),
    ("Tools/Run Command…", "execute_command"),
    ("Tools/Evaluate Buffer", "eval_buffer"),
    ("Tools/Evaluate Definition", "eval_definition"),
    ("Tools/Python REPL", "repl"),
    ("Tools/Toggle Breakpoint", "debug.toggle_breakpoint"),
    ("Tools/Inspect Last Error", "debug.inspect"),
    ("Help/Describe Key…", "describe_key"),
    ("Help/Describe Command…", "describe_command"),
    ("Help/Search Commands…", "apropos"),
]


def install_menu(menu: object) -> None:
    """Add the standard items to an editor's menu."""
    from neomacs.menu import Menu

    assert isinstance(menu, Menu)
    for path, command_name in MENU:
        menu.add(path, command_name)
