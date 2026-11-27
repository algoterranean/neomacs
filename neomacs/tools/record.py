"""Record a scripted session as a protocol conformance fixture (ENGINE 14).

    python -m neomacs.tools.record tests/data/sessions/basic.json

The file holds every `redraw` batch the engine sent, in order, and what a
correct frontend holds afterwards.  Any frontend can replay the batches into
its own model and compare.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from neomacs.testing.client import running_engine
from neomacs.testing.headless import HeadlessUI

ROWS, COLS = 12, 40


def script(ui: HeadlessUI) -> None:
    """The session that is recorded.  Extend it as the protocol grows."""
    ui.eval(f"_b = ed.buffers.new('recorded'); ed.frames[{ui.frame_id}].window.show(_b)")
    ui.type("def greet(name):\n\treturn 'héllo ' + name  # 中文 👍🏽\n")
    ui.eval("_b.layer('syntax').add(0, 3, face='keyword')")
    ui.eval("_b.layer('syntax').add(_b.line_start(2) + 8, _b.line_start(2) + 16, face='string')")
    ui.eval("_b.insert(len(_b), '\\n'.join(f'line {n} ' + 'x' * (n % 50) for n in range(300)))")
    ui.keys("M-<", "<down>", "<end>")
    ui.type(" edited")
    ui.keys("M->")
    ui.scroll(-100)
    ui.idle()
    ui.eval("_b.insert(0, '# a new first line\\n')")
    ui.keys("<prior>", "<up>", "<up>")
    ui.eval("_b.delete(_b.line_start(100), _b.line_start(200))")
    ui.eval("ed.faces.define('keyword', fg='#ff8800', weight='bold')")
    ui.eval("ed.faces.define('keyword', fg='#c586c0')")


def summarise(ui: HeadlessUI) -> dict[str, Any]:
    with ui.lock:
        views: dict[str, Any] = {}
        for view in ui.model.views.values():
            views[str(view.id)] = {
                "info": view.info,
                "first": view.first,
                "lines": view.lines,
                "selections": view.selections,
                "primary": view.primary,
                "shape": view.shape,
                "modeline": view.modeline,
            }
        return {
            "faces": {str(k): v for k, v in sorted(ui.model.faces.items())},
            "ui_faces": ui.model.ui_faces,
            "layout": ui.model.layout,
            "focus": ui.model.focus,
            "title": ui.model.title,
            "views": views,
        }


def record() -> dict[str, Any]:
    batches: list[Any] = []
    with running_engine() as path, HeadlessUI(path, rows=ROWS, cols=COLS) as ui:
        original = ui.client.on_notification
        assert original is not None

        # The first batches arrived during attach; ask for them again so the
        # recording starts from a reset.
        def tap(method: str, params: list[Any]) -> None:
            if method == "redraw":
                batches.append(params[0])
            original(method, params)

        ui.client.on_notification = tap
        ui.client.call("ui.resync")
        ui.idle()
        script(ui)
        ui.idle()
        return {
            "format": 1,
            "protocol": 1,
            "rows": ROWS,
            "cols": COLS,
            "batches": batches,
            "final": summarise(ui),
            "screen": ui.screen(),
        }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(args[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record(), ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
