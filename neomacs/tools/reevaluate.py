"""Gate D: re-evaluate the editor's own modules in a running engine.

`all_modules` visits every module under `neomacs/std/`, and `neomacs/modes.py`,
and evaluates each as `eval_buffer` would.  With NEOMACS_REEVALUATE set in
the environment an engine does this before it serves anyone, so the whole
test suite can then be run against engines made of live-patched code:

    python -m neomacs.tools.reevaluate          # checks an engine, then runs the suite that way
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def module_files() -> list[Path]:
    files = sorted((ROOT / "std").glob("*.py"))
    files.append(ROOT / "modes.py")
    if (ROOT / "modes").is_dir():
        files.extend(sorted((ROOT / "modes").rglob("*.py")))
    return files


async def all_modules(rounds: int = 2) -> list[tuple[str, str, str]]:
    """Evaluate every module ROUNDS times; return the fallbacks to rebinding."""
    from neomacs.editor import current
    from neomacs.live import run

    ed = current()
    before = len(ed.eval_warnings)
    for _ in range(rounds):
        for path in module_files():
            buffer = ed.buffers.visit(path)
            await run.eval_range(buffer, 0, len(buffer), whole=True, mode="exec")
            ed.buffers.kill(buffer)
    ed.reevaluated = rounds * len(module_files())
    return ed.eval_warnings[before:]


PROBE = """
import neomacs.std.movement as _mv, neomacs.modes as _modes, neomacs.minibuffer as _mb
_fn = ed.commands['forward_char'].fn
{
    'evaluations': ed.reevaluated,
    'warnings': ed.eval_warnings,
    # The registry holds the newest function; the module still holds the first
    # one, now running the newest code, which was compiled from the buffer.
    'module_keeps_its_object': _mv.forward_char is not _fn,
    'and_runs_the_new_code': _mv.forward_char.__code__ is _fn.__code__,
    'classes_kept': _modes.FundamentalMode.__mro__[1] is _modes.Mode
    and issubclass(_mb.MinibufferMode, _modes.Mode),
    'commands': len(ed.commands),
    'menu_items': sum(len(m.get('children', [])) for m in ed.menu.tree([])),
}
"""


def probe(reevaluate: bool) -> dict[str, object]:
    """Start an engine and ask it what re-evaluation did to it."""
    from neomacs.testing import Client, running_engine

    env = {"NEOMACS_REEVALUATE": "1" if reevaluate else ""}
    with running_engine(env=env) as path, Client(path) as client:
        return eval(client.eval(PROBE))  # noqa: S307 - a repr from our own engine


def main() -> int:
    plain, patched = probe(False), probe(True)
    files = module_files()
    print(f"modules: {len(files)}, each evaluated twice in every engine before it serves")
    print(f"engine as imported:  {plain}")
    print(f"engine re-evaluated: {patched}")
    problems: list[str] = []
    if patched["evaluations"] != 2 * len(files):
        problems.append("the modules were not all evaluated")
    if not (patched["module_keeps_its_object"] and patched["and_runs_the_new_code"]):
        problems.append("functions were not patched in place")
    if not patched["classes_kept"]:
        problems.append("classes were not patched in place")
    if patched["warnings"]:
        problems.append(f"fallbacks to rebinding: {patched['warnings']}")
    for key in ("commands", "menu_items"):
        if patched[key] != plain[key]:
            problems.append(f"{key} changed: {plain[key]} before, {patched[key]} after")
    env = dict(os.environ, NEOMACS_REEVALUATE="1")
    status = subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env)
    if status != 0:
        problems.append("the test suite failed against re-evaluated engines")
    print("GATE D: " + ("passed" if not problems else "FAILED: " + "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
