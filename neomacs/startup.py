"""The user's own code, loaded when the engine starts (EXTENSIONS 3.2, 3.3, 6.1).

Single-file extensions in name order, then `init.py`, each through the live
evaluation pipeline: they are modules with an ownership record, so evaluating
one again from a buffer replaces what it registered before.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs import paths
from neomacs.hooks import ui_attached
from neomacs.live import run
from neomacs.live.evaluate import evaluate

if TYPE_CHECKING:
    from neomacs.editor import Editor

USER_PACKAGE = "neomacs_user"  # single-file extensions are its submodules
INIT_MODULE = "neomacs_init"


def _package(directory: Path) -> types.ModuleType:
    """The package the single-file extensions belong to.  It has no file of its
    own; its path is their directory, so one of them can import another."""
    package = sys.modules.get(USER_PACKAGE)
    if package is None:
        package = types.ModuleType(USER_PACKAGE)
        package.__package__ = USER_PACKAGE
        sys.modules[USER_PACKAGE] = package
    package.__path__ = [str(directory)]
    return package


async def load_module(editor: Editor, path: Path, name: str, what: str) -> bool:
    """Evaluate the file at PATH as the module NAME.  A failure is reported,
    remembered for the first frame to be told, and does not stop the rest."""
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__file__ = str(path)
        module.__package__ = name.rpartition(".")[0]
        sys.modules[name] = module
        parent = sys.modules.get(module.__package__) if module.__package__ else None
        if parent is not None:
            setattr(parent, name.rpartition(".")[2], module)
    try:
        text = path.read_text(encoding="utf-8")
        result = await evaluate(
            text, module.__dict__, filename=str(path), mode="exec", whole_module=True
        )
    except Exception as exc:
        editor.report_exception(f"loading {what}", exc, keep=True)
        editor.startup_problems.append(f"{what} failed to load: {type(exc).__name__}: {exc}")
        return False
    run.announce(name, result)
    return True


def extension_files(directory: Path) -> list[Path]:
    """The single-file extensions in DIRECTORY, in the order they load."""
    try:
        found = [p for p in directory.iterdir() if p.suffix == ".py" and p.is_file()]
    except OSError:
        return []
    return sorted(
        (p for p in found if p.stem.isidentifier() and not p.stem.startswith("_")),
        key=lambda p: p.stem,
    )


async def load_user_code(editor: Editor) -> None:
    """Load the single-file extensions and then the init file, unless started with `--safe`."""
    if editor.safe:
        editor.message("Started with --safe: no extensions and no init file", kind="log")
        return
    directory = paths.extensions_dir()
    files = extension_files(directory)
    if files:
        _package(directory)
    for path in files:
        name = f"{USER_PACKAGE}.{path.stem}"
        if name in sys.modules:
            continue  # another extension imported it already
        await load_module(editor, path, name, f"extension {path.stem}")
    init = paths.init_file()
    if init.is_file():
        await load_module(editor, init, INIT_MODULE, "init.py")


def _tell_first_frame(frame: Any) -> None:
    """Nobody was looking when the user's code was loaded: say now what went wrong."""
    editor: Editor = frame.editor
    problems, editor.startup_problems = editor.startup_problems, []
    if not problems:
        return
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    editor.message(f"{problems[0]}{more}; see *messages*", kind="error", frame=frame)


ui_attached.add(_tell_first_frame)
