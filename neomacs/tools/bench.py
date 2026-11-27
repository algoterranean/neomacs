"""The latency benchmark (ENGINE 14, Gate B of doc/IMPLEMENTATION.md).

    python -m neomacs.tools.bench             print the measurements
    python -m neomacs.tools.bench --check     also exit 1 if a 99th percentile is over the limit

It measures, inside the engine, the time from an `input.key` line arriving to
the `redraw` batch that shows its effect having been handed to the writer:
JSON parsing, dispatch, the command, the view update and JSON encoding.  It
does not include the socket or the frontend.

Keys are sent one at a time, each after the engine has gone idle, so every key
gets a batch of its own.  The buffer is a 10,000-line file with the cursor in
the middle.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from neomacs.testing.client import running_engine
from neomacs.testing.headless import HeadlessUI

LINES = 10_000
LIMIT_MS = 5.0

HIGHLIGHT = """
import re as _re
_words = _re.compile(r"\\b(def|return|if|else|for|in|import|from|class|self|None|True|False)\\b"
                     r"|(#.*)|(\\"[^\\"]*\\"|'[^']*')")
def _highlight(buffer, start, end):
    text = buffer[start:end]
    for m in _words.finditer(text):
        face = "keyword" if m.group(1) else "comment" if m.group(2) else "string"
        yield (start + m.start(), start + m.end(), face, 0)
_bench.providers.append(_highlight)
_bench.invalidate(0, len(_bench))
"""


def source() -> str:
    """Ten thousand lines that look like code: mixed lengths, some long enough to wrap."""
    lines: list[str] = []
    n = 0
    while len(lines) < LINES:
        n += 1
        lines.append(f"def function_{n}(self, first, second=None):")
        lines.append(f'    """Docstring number {n}, saying what the function is for."""')
        lines.append(f"    value = first.compute(second, 'literal {n}') + {n}  # a comment here")
        lines.append("    if value is None or value > self.limit:")
        lines.append(
            "        return self.fallback(value, " + ", ".join(f"arg{i}" for i in range(12)) + ")"
        )
        lines.append("    return value")
        lines.append("")
    return "\n".join(lines[:LINES]) + "\n"


def percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summarise(samples: list[float]) -> dict[str, float]:
    ms = [s * 1000 for s in samples]
    return {
        "n": len(ms),
        "p50": round(statistics.median(ms), 3),
        "p90": round(percentile(ms, 0.90), 3),
        "p99": round(percentile(ms, 0.99), 3),
        "max": round(max(ms), 3),
        "mean": round(statistics.fmean(ms), 3),
    }


def measure(ui: HeadlessUI, keys: list[str], warmup: int = 50) -> dict[str, Any]:
    """Time each of KEYS twice over: inside the engine, and as this client sees it."""
    for key in keys[:warmup]:
        ui.keys(key)
    ui.client.eval("ed.latency = []")
    seen = threading.Event()
    ui.on_redraw = seen.set
    round_trips: list[float] = []
    for key in keys:
        seen.clear()
        sent = time.perf_counter()
        ui.client.notify("input.key", key)
        if seen.wait(5):
            # Send, engine, receive, and this client's own parsing and layout.
            round_trips.append(time.perf_counter() - sent)
        ui.idle()
    ui.on_redraw = None
    samples: list[float] = json.loads(ui.client.eval("__import__('json').dumps(ed.latency)")[1:-1])
    ui.client.eval("ed.latency = None")
    return {"engine": summarise(samples), "round_trip": summarise(round_trips)}


def run(count: int) -> dict[str, Any]:
    typing = [("abcdefghij klmnop"[i % 17]) for i in range(count)]
    typing = ["<space>" if k == " " else k for k in typing]
    mixed = [(k if i % 9 else "<return>") for i, k in enumerate(typing)]
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp, running_engine() as path:
        file = Path(tmp) / "bench.py"
        file.write_text(source(), encoding="utf-8")
        with HeadlessUI(path, rows=50, cols=120) as ui:
            ui.call("files.find", str(file))
            ui.eval(
                f"_bench = ed.buffers.find_file({str(file)!r}); ed.options.typing_undo_limit = 20"
            )
            ui.call("goto_line", LINES // 2)
            ui.keys("C-e")
            results["gil_enabled"] = ui.client.eval("__import__('sys')._is_gil_enabled()")
            results["self_insert"] = measure(ui, typing)
            results["self_insert_and_newlines"] = measure(ui, mixed)
            ui.keys("C-M-n", "C-M-n")
            results["three_cursors"] = measure(ui, typing)
            ui.keys("C-g")
            ui.eval(HIGHLIGHT)
            # Type on a line of code that has keywords, a string and a comment,
            # in a part of the file the earlier scenarios did not fill with letters.
            ui.call("goto_line", 2103)
            ui.keys("C-e")
            shown = [len((ui.view.line(n) or [{}, []])[1]) for n in range(2100, 2114)]
            results["highlighted_runs_per_line"] = round(sum(shown) / len(shown), 1)
            results["highlighted_runs_on_typed_line"] = shown[3]
            results["with_simulated_highlighting"] = measure(ui, typing)
            ui.keys("M-<")
            results["at_top_of_file"] = measure(ui, typing)
            ui.eval("_bench.insert(0, 'x = [' + ', '.join(str(n) for n in range(600)) + ']\\n')")
            ui.keys("M-<", "C-e")
            results["in_a_3000_character_line"] = measure(ui, typing)
            # A burst: keys sent without waiting, as held-down key repeat would.
            started = time.perf_counter()
            batches = ui.batches
            for key in typing:
                ui.client.notify("input.key", key)
            ui.idle(timeout=60)
            elapsed = time.perf_counter() - started
            results["burst"] = {
                "keys": len(typing),
                "seconds": round(elapsed, 3),
                "ms_per_key": round(elapsed * 1000 / len(typing), 3),
                "batches": ui.batches - batches,
            }
            ui.client.notify("ui.detach")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neomacs.tools.bench")
    parser.add_argument("--keys", type=int, default=1000, help="keys per scenario")
    parser.add_argument("--check", action="store_true", help="fail if a p99 exceeds the limit")
    parser.add_argument("--limit-ms", type=float, default=LIMIT_MS)
    args = parser.parse_args(argv)
    results = run(args.keys)
    print(json.dumps(results, indent=1))
    if not args.check:
        return 0
    over: list[str] = []
    for name, result in results.items():
        if isinstance(result, dict) and "engine" in result:
            engine = cast(dict[str, dict[str, float]], result)["engine"]
            if engine["p99"] > args.limit_ms:
                over.append(name)
    if over:
        print(f"p99 over {args.limit_ms} ms in: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
