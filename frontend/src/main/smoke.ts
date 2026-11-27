// A scripted run of the real app against a real engine: the checks for F0, F1 and Gate A
// (doc/IMPLEMENTATION.md).  Prints one line, `SMOKE-RESULT {json}`, and exits 0 or 1.

import type { BrowserWindow } from "electron";
import { mkdtempSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { Control } from "./engine.ts";

interface State {
  window: BrowserWindow;
  frame: number | null;
}

interface ViewSnapshot {
  lineCount: number;
  held: [number, number] | null;
  dom: [number, number];
  domElements: number;
  visible: [number, number];
  missingVisible: number;
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  estimate: number;
  firstVisibleText: string | null;
  firstVisibleY: number | null;
  modeline: string | null;
}

interface Snapshot {
  frame: number | null;
  batches: number;
  renders: number;
  resets: number;
  faces: number;
  view: ViewSnapshot | null;
}

const LINES = 100_000;

function bigFile(): string {
  const dir = mkdtempSync(path.join(os.tmpdir(), "nm-smoke-"));
  const file = path.join(dir, "big.txt");
  const parts: string[] = [];
  for (let n = 1; n <= LINES; n++) {
    let line = `line ${n}`;
    if (n % 7 === 0) line += " " + "wrapping words that go on and on ".repeat(12);
    if (n % 11 === 0) line += " — naïve café 中文 👍🏽";
    parts.push(line);
  }
  writeFileSync(file, parts.join("\n"));
  return file;
}

export async function runSmoke(
  state: State,
  control: Control,
  whenAttached: (state: never) => Promise<void>,
): Promise<number> {
  const results: Record<string, unknown> = {};
  const failures: string[] = [];
  const check = (name: string, ok: boolean, detail?: unknown) => {
    results[name] = detail === undefined ? ok : { ok, detail };
    if (!ok) failures.push(name);
  };
  const js = <T>(code: string) => state.window.webContents.executeJavaScript(code, true) as Promise<T>;
  const snap = () => js<Snapshot>("window.neomacsDebug.snapshot()");
  const frames = (n: number) => js<void>(`window.neomacsDebug.frames(${n})`);
  const settle = async () => {
    for (let i = 0; i < 3; i++) {
      await control.call("engine.idle", 10);
      await frames(3);
    }
  };
  const view = async () => {
    const s = await snap();
    if (!s.view) throw new Error("no view is mounted");
    return s.view;
  };
  const timer = setTimeout(() => {
    console.log("SMOKE-RESULT " + JSON.stringify({ ok: false, failures: ["timed out"], results }));
    process.exit(1);
  }, 120_000);

  try {
    await whenAttached(state as never);
    const frame = state.frame as number;
    const win = `ed.frames[${frame}].window`;

    // F0: connected, and the engine is who we think it is.
    const info = (await control.call("engine.api_info")) as { protocol: number; version: string };
    check("f0.api_info", info.protocol === 1, info.version);

    // F1: a large file, in a proportional font, with a second face at a larger size.
    const file = bigFile();
    await control.eval(
      `ed.faces.define('default', family='Helvetica Neue', fg='#d4d4d4', bg='#1e1e1e')\n` +
        `_big = ed.buffers.visit(${JSON.stringify(file)})\n` +
        `${win}.show(_big)\n` +
        `_big.layer('smoke').add(0, _big.line_end(1), face='heading-1')`,
    );
    await settle();
    let v = await view();
    check("f1.line_count", v.lineCount === LINES, v.lineCount);
    check("f1.first_line", v.firstVisibleText === "line 1", v.firstVisibleText);
    check("f1.dom_is_virtual", v.domElements > 5 && v.domElements < 400, v.domElements);
    check("f1.held_is_partial", v.held !== null && v.held[1] < 2000, v.held);
    const fonts = await js<{ family: string; narrow: number; wide: number; base: number; heading: number }>(`(() => {
      const content = document.querySelector('.nm-line .nm-content');
      const measure = (text) => { const s = document.createElement('span'); s.textContent = text;
        content.append(s); const w = s.getBoundingClientRect().width; s.remove(); return w; };
      const heading = document.querySelector('.nm-line .nm-content span[class^="f"]');
      return { family: getComputedStyle(content).fontFamily, narrow: measure('iiiiiiii'), wide: measure('MMMMMMMM'),
               base: parseFloat(getComputedStyle(document.querySelector('.nm-frame')).fontSize),
               heading: heading ? parseFloat(getComputedStyle(heading).fontSize) : 0 };
    })()`);
    check("f1.proportional_font", fonts.wide > fonts.narrow * 1.5, fonts);
    check("f1.larger_face", Math.abs(fonts.heading / fonts.base - 1.6) < 0.05, fonts.heading / fonts.base);

    // Gate A1: scrolling.  The run happens inside the page, one step per frame, and
    // counts frames that left a visible line without an element or part of the
    // viewport uncovered.
    interface Run { step: number; pxPerSecond: number; badFrames: number; longestRun: number; worstBlank: number; worstMissing: number }
    const scrollRun = (step: number, steps: number) => js<Run>(`window.neomacsDebug.scrollRun(${step}, ${steps})`);
    const speeds: Run[] = [];
    for (const step of [20, 60, 120, 240]) {
      speeds.push(await scrollRun(step, 120));
      await settle();
    }
    results["gateA.scroll_speeds"] = speeds;
    const ordinary = speeds.filter((r) => r.step <= 120);
    check("gateA.scroll_ordinary", ordinary.every((r) => r.longestRun <= 2), ordinary.map((r) => [r.pxPerSecond, r.badFrames, r.longestRun]));
    const fast = await scrollRun(1500, 60);
    results["gateA.scroll_fast"] = fast; // reported, not a pass or fail
    await settle();
    v = await view();
    check("gateA.scroll_settles", v.missingVisible === 0 && v.visible[0] > 100, v.visible);
    const settled = await js<{ missingDom: number; blank: number }>("window.neomacsDebug.coverage()");
    check("gateA.scroll_settles_fully_drawn", settled.missingDom === 0 && settled.blank <= 1, settled);

    // Gate A1b: scrolling back up over lines whose heights were only estimated.  As
    // they are measured (the wrapped ones are much taller than the estimate) the
    // text must still move by exactly what was scrolled.
    interface Steady { measured: number; worstJump: number }
    const up = await js<Steady>("window.neomacsDebug.steadyRun(-90, 80)");
    check("gateA.steady_scrolling_up", up.measured > 60 && up.worstJump <= 1, up);
    await settle();
    const down = await js<Steady>("window.neomacsDebug.steadyRun(90, 80)");
    check("gateA.steady_scrolling_down", down.measured > 60 && down.worstJump <= 1, down);
    await settle();

    // Jump to the end.
    await js<void>("window.neomacsDebug.scrollTo(1e12)");
    await settle();
    v = await view();
    check("f1.end_of_file", v.visible[1] === LINES && v.missingVisible === 0, v.visible);
    const lastText = await js<string | null>(
      `document.querySelector('.nm-lines [data-line="${LINES}"] .nm-content')?.textContent ?? null`,
    );
    check("f1.last_line_text", lastText !== null && lastText.startsWith(`line ${LINES}`), lastText);

    // Gate A2: edits above the viewport must not move what is on screen.
    await control.eval(`${win}.reveal(_big.line_start(5000), 'top')`);
    await settle();
    await js<void>("window.neomacsDebug.scrollBy(7)"); // part-way into a line
    await settle();
    const before = await view();
    await control.eval(
      `_big.insert(0, 'three\\nnew\\nlines\\n')\n` +
        `_big.insert(_big.line_start(4000), 'made much longer so that it wraps ' * 20)\n` +
        `_big.delete(_big.line_start(100), _big.line_start(150))`,
    );
    await settle();
    const after = await view();
    check(
      "gateA.edit_above_keeps_view",
      after.firstVisibleText === before.firstVisibleText &&
        before.firstVisibleY !== null &&
        after.firstVisibleY !== null &&
        Math.abs(after.firstVisibleY - before.firstVisibleY) <= 1,
      { before: [before.firstVisibleText, before.firstVisibleY, before.visible], after: [after.firstVisibleText, after.firstVisibleY, after.visible] },
    );
    check("gateA.edit_above_renumbers", after.visible[0] === before.visible[0] + 3 - 50, [before.visible[0], after.visible[0]]);

    // Gate A2b: the harder case.  Change lines that are laid out and measured, just
    // above the viewport: one grows from one row to many, one shrinks, one is
    // inserted directly above the first visible line.
    const base = await view();
    const top = base.visible[0];
    const baseY = await js<number | null>(`window.neomacsDebug.lineY(${top})`);
    await control.eval(
      `_big.insert(_big.line_start(${top - 3}), 'now a very long line that wraps over several rows ' * 15)\n` +
        `_big.delete(_big.line_start(${top - 6}), _big.line_end(${top - 6}))\n` +
        `_big.insert(_big.line_start(${top}), 'inserted directly above\\n')`,
    );
    await settle();
    const near = await view();
    const nearY = await js<number | null>(`window.neomacsDebug.lineY(${top + 1})`);
    check(
      "gateA.edit_just_above_keeps_view",
      near.firstVisibleText === base.firstVisibleText && baseY !== null && nearY !== null && Math.abs(nearY - baseY) <= 1,
      { text: [base.firstVisibleText, near.firstVisibleText], y: [baseY, nearY], top: [top, near.visible[0]] },
    );
    const aboveText = await js<string | null>(
      `document.querySelector('.nm-lines [data-line="${top}"] .nm-content')?.textContent ?? null`,
    );
    check("gateA.edit_just_above_is_drawn", aboveText === "inserted directly above", aboveText);

    // Gate A2c: typing inside a wrapped line on screen makes it taller; lines above it,
    // and the top of the viewport, stay put.
    const typedLine = near.visible[0] + 4;
    const topY = await js<number | null>(`window.neomacsDebug.lineY(${near.visible[0]})`);
    await control.eval(`_big.insert(_big.line_start(${typedLine}), 'typed text that makes this line wrap onto more rows ' * 8)`);
    await settle();
    const topY2 = await js<number | null>(`window.neomacsDebug.lineY(${near.visible[0]})`);
    check("gateA.edit_in_view_keeps_top", topY !== null && topY2 !== null && Math.abs(topY2 - topY) <= 1, [topY, topY2]);

    // An edit on screen changes just that line.
    const target = after.visible[0] + 2;
    await control.eval(`_big.insert(_big.line_start(${target}), 'EDITED ')`);
    await settle();
    const edited = await js<string | null>(
      `document.querySelector('.nm-lines [data-line="${target}"] .nm-content')?.textContent ?? null`,
    );
    check("f1.edit_on_screen", edited !== null && edited.startsWith("EDITED line"), edited);

    // F1: reload the page; the connection and the frame survive, and the display comes back.
    const beforeReload = await view();
    const resetsBefore = (await snap()).resets;
    state.window.webContents.reload();
    await new Promise((resolve) => setTimeout(resolve, 300));
    for (let i = 0; i < 50 && (await js<boolean>("!!(window.neomacsDebug && window.neomacsDebug.snapshot().view)").catch(() => false)) === false; i++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    await settle();
    const afterReload = await snap();
    check("f1.reload_same_frame", afterReload.frame === frame, afterReload.frame);
    check(
      "f1.reload_same_display",
      afterReload.view !== null &&
        afterReload.view.firstVisibleText === beforeReload.firstVisibleText &&
        afterReload.view.visible[0] === beforeReload.visible[0],
      { before: [beforeReload.firstVisibleText, beforeReload.visible], after: [afterReload.view?.firstVisibleText, afterReload.view?.visible], resetsBefore },
    );

    // ---------------------------------------------------------------------------
    // Step 6 (F2): keys, cursors and selections, prompts, input-method text.
    // Keys are real key events delivered to the page by Chromium.
    const contents = state.window.webContents;
    contents.focus();
    await js<void>("document.getElementById('root').focus()");
    const press = (keyCode: string, modifiers: ("shift" | "control" | "alt" | "meta")[] = []) => {
      contents.sendInputEvent({ type: "keyDown", keyCode, modifiers });
      contents.sendInputEvent({ type: "keyUp", keyCode, modifiers });
    };
    const type = (text: string) => {
      for (const ch of text) press(ch === " " ? "Space" : ch);
    };
    const pyText = async (expr: string) => (await control.eval(expr)) as string;
    // A value from the engine as JSON, by way of base64 so no quoting can go wrong.
    const pyJson = async <T>(expr: string): Promise<T> => {
      const repr = (await control.eval(`__import__('base64').b64encode(__import__('json').dumps(${expr}).encode()).decode()`)) as string;
      return JSON.parse(Buffer.from(repr.slice(1, -1), "base64").toString("utf8")) as T;
    };
    interface Ui {
      focus: number | null;
      mainView: number | null;
      selections: [number, number, number, number][];
      view: ViewSnapshot & { cursors: number; selections: number; cursorAnimated: boolean };
      chrome: { prompt: string | null; promptText: string | null; popup: string[] | null; popupSelected: string | null; message: string; keys: string };
      input: { keys: number; texts: number; editContext: boolean };
      activeIsRoot: boolean;
    }
    const ui = () => js<Ui>("window.neomacsDebug.snapshot()");

    await control.eval(`ed.faces.define('default', fg='#d4d4d4', bg='#1e1e1e')\n_k = ed.buffers.new('keys')\n${win}.show(_k)`);
    await settle();
    type("hello world");
    await settle();
    let u = await ui();
    check("f2.focus_is_on_the_page", u.activeIsRoot && u.input.editContext, [u.activeIsRoot, u.input.editContext]);
    check("f2.typing", (await pyText("_k.text")) === "'hello world'", [await pyText("_k.text"), u.input.keys]);
    check("f2.cursor_drawn", u.view.cursors === 1, u.view.cursors);
    press("Home");
    press("f", ["alt"]);
    await settle();
    u = await ui();
    check("f2.meta_key", JSON.stringify(u.selections) === "[[1,5,1,5]]", u.selections);
    press("Right", ["shift"]);
    press("Right", ["shift"]);
    press("Right", ["shift"]);
    await settle();
    u = await ui();
    check("f2.shift_selects_and_is_drawn", JSON.stringify(u.selections) === "[[1,5,1,8]]" && u.view.selections >= 1, [u.selections, u.view.selections]);
    press("q", ["control"]);
    await settle();
    u = await ui();
    check("f2.message_strip", u.chrome.message === "C-q is undefined", u.chrome.message);

    // Several cursors.
    await control.eval(`_k.replace(0, len(_k), 'aaa\\nbbb\\nccc'); ${win}.selections.set([(0, 0)])`);
    await settle();
    press("n", ["control", "alt"]);
    press("n", ["control", "alt"]);
    type("xy");
    await settle();
    u = await ui();
    check("f2.three_cursors_edit_three_places", (await pyText("_k.text")) === "'xyaaa\\nxybbb\\nxyccc'" && u.view.cursors === 3, [await pyText("_k.text"), u.view.cursors]);
    check("f2.cursor_glides", u.view.cursorAnimated, u.view.cursorAnimated);
    await control.eval(`_k.replace(0, len(_k), '\\n'.join('line' for _ in range(12))); ${win}.selections.set([(n * 5, n * 5) for n in range(12)])`);
    await settle();
    press("Right");
    await settle();
    u = await ui();
    check("f2.many_cursors_do_not_animate", u.view.cursors === 12 && !u.view.cursorAnimated, [u.view.cursors, u.view.cursorAnimated]);
    press("g", ["control"]);
    await settle();

    // A prompt with completion, then the prompt of the command it ran.
    press("x", ["alt"]);
    await settle();
    u = await ui();
    check("f2.prompt_opens", u.chrome.prompt === "M-x " && (u.chrome.popup?.length ?? 0) > 10 && u.focus !== u.mainView, [u.chrome.prompt, u.chrome.popup?.length]);
    type("goto");
    await settle();
    u = await ui();
    check("f2.completion_narrows", JSON.stringify(u.chrome.popup) === '["goto_line"]' && u.chrome.promptText === "goto", [u.chrome.popup, u.chrome.promptText]);
    press("Return");
    await settle();
    u = await ui();
    check("f2.nested_prompt", u.chrome.prompt === "Go to line: " && u.chrome.popup === null, [u.chrome.prompt, u.chrome.popup]);
    type("7");
    press("Return");
    await settle();
    u = await ui();
    check("f2.prompt_answered", u.chrome.prompt === null && JSON.stringify(u.selections) === "[[7,0,7,0]]" && u.focus === u.mainView, [u.chrome.prompt, u.selections]);

    // Text committed through the EditContext, as an input method would.
    await contents.insertText("日本語");
    await settle();
    u = await ui();
    check("f2.committed_text", (await pyText("_k.line_text(7)")) === "'日本語line'" && u.input.texts >= 1, [await pyText("_k.line_text(7)"), u.input.texts]);

    // Visual-line motion in wrapped proportional text, across lines of different sizes.
    await control.eval(
      `ed.faces.define('default', family='Helvetica Neue', fg='#d4d4d4', bg='#1e1e1e')\n` +
        `_words = ' '.join(['the quick brown fox jumps over the lazy dog'] * 12)\n` +
        `_m = ed.buffers.new('mixed', '\\n'.join([_words] * 8))\n` +
        `${win}.show(_m)\n` +
        `for _n in (2, 4, 6):\n    _m.layer('h').add(_m.line_start(_n), _m.line_end(_n), face='heading-1')\n` +
        `${win}.selections.set([(40, 40)])`,
    );
    await settle();
    interface CursorX { x: number; line: number; offset: number; height: number }
    const cursorX = () => js<CursorX | null>("window.neomacsDebug.cursorX()");
    const start = await cursorX();
    press("Down");
    await settle();
    const oneDown = await cursorX();
    check(
      "f2.visual_line_motion_in_a_wrapped_line",
      start !== null && oneDown !== null && oneDown.line === 1 && oneDown.offset > start.offset + 20 && Math.abs(oneDown.x - start.x) <= 24,
      { start, oneDown },
    );

    // Gate C2: the horizontal goal survives rows of different heights and fonts.
    const walk: CursorX[] = [];
    for (let i = 0; i < 24; i++) {
      press("Down");
      await settle();
      const c = await cursorX();
      if (c) walk.push(c);
    }
    const heights = new Set(walk.map((c) => Math.round(c.height)));
    const worstDrift = start ? Math.max(...walk.map((c) => Math.abs(c.x - start.x))) : Infinity;
    check("gateC.rows_differ_in_height", heights.size >= 2 && Math.max(...heights) > 1.4 * Math.min(...heights), [...heights]);
    check("gateC.goal_kept_going_down", walk.length === 24 && new Set(walk.map((c) => c.line)).size >= 4 && worstDrift <= 24, { worstDrift, lines: [...new Set(walk.map((c) => c.line))] });
    for (let i = 0; i < 25; i++) press("Up");
    await settle();
    const back = await cursorX();
    check("gateC.goal_kept_coming_back", start !== null && back !== null && back.line === start.line && back.offset === start.offset, { start, back });

    // Gate C3: what a vertical motion costs, from the key arriving at the engine
    // to its redraw leaving, including the round trip to this frontend.
    await control.eval("ed.latency = []; ed.request_times = []");
    for (let i = 0; i < 240; i++) {
      press(Math.floor(i / 12) % 2 === 0 ? "Down" : "Up");
      await control.call("engine.idle", 10);
    }
    const timings = await pyJson<[number[], number[]]>("[ed.latency, ed.request_times]");
    await control.eval("ed.latency = None; ed.request_times = None");
    const stats = (seconds: number[]) => {
      const ms = seconds.map((s) => s * 1000).sort((a, b) => a - b);
      const at = (q: number) => Math.round((ms[Math.min(ms.length - 1, Math.floor(q * ms.length))] ?? 0) * 100) / 100;
      return { n: ms.length, p50: at(0.5), p90: at(0.9), p99: at(0.99), max: at(1) };
    };
    const keyToFlush = stats(timings[0]);
    const roundTrip = stats(timings[1]);
    results["gateC.round_trip_ms"] = roundTrip;
    check("gateC.vertical_motion_within_budget", keyToFlush.n >= 200 && roundTrip.n >= 200 && keyToFlush.p99 < 5, keyToFlush);

    // Gate C1: with a monospaced face and ASCII text, every answer equals the reference layout's.
    await control.eval(
      `ed.faces.define('default', fg='#d4d4d4', bg='#1e1e1e')\n` +
        `from neomacs.tools import geometry_check as _gc\n` +
        `_g = ed.buffers.new('geometry', _gc.sample_text())\n${win}.show(_g)\n_geo = None`,
    );
    await settle();
    const metrics = await js<{ charWidth: number; lineHeight: number; rows: number; contentWidth: number }>("window.neomacsDebug.metrics()");
    const gutterPx = await js<number>(
      "document.querySelector('.nm-line .nm-content').getBoundingClientRect().left - document.querySelector('.nm-scroller').getBoundingClientRect().left",
    );
    const cols = Math.floor(metrics.contentWidth / metrics.charWidth + 1e-6);
    await control.eval(
      `import asyncio\n` +
        `async def _run_geo():\n` +
        `    global _geo\n` +
        `    try:\n` +
        `        _geo = await _gc.compare(ed.frames[${frame}], ${cols}, ${metrics.rows}, ${metrics.charWidth}, ${metrics.lineHeight}, ${gutterPx})\n` +
        `    except Exception as exc:\n` +
        `        _geo = {'error': repr(exc)}\n` +
        `ed.start_task(_run_geo())`,
    );
    interface Geo { error?: string; total?: number; mismatched?: number; checked?: Record<string, number>; mismatches?: unknown[] }
    const waitForGeo = async (): Promise<Geo | null> => {
      for (let i = 0; i < 900; i++) {
        await new Promise((resolve) => setTimeout(resolve, 100));
        const found = await pyJson<Geo | null>("_geo");
        if (found !== null) return found;
      }
      return null;
    };
    const geo = await waitForGeo();
    results["gateC.geometry_metrics"] = { ...metrics, cols, gutterPx };
    check(
      "gateC.answers_equal_the_reference",
      geo !== null && geo.error === undefined && (geo.total ?? 0) > 1000 && geo.mismatched === 0,
      geo === null ? "no result" : { total: geo.total, mismatched: geo.mismatched, checked: geo.checked, error: geo.error, first: geo.mismatches?.slice(0, 6) },
    );

    // A geometry question about a line the frontend does not hold: it has to
    // fetch the line, answer, and leave the view where it was.
    await control.eval(`${win}.show(_big); ${win}.reveal(_big.line_start(200), 'top')`);
    await settle();
    const beforeFar = await view();
    await control.eval(
      `_far = None\n` +
        // Two short lines in a row, far from anything the frontend holds.
        `_far_line = next(n for n in range(60000, 60100) if len(_big.line_text(n)) < 30 and len(_big.line_text(n + 1)) < 30)\n` +
        `async def _ask_far():\n` +
        `    global _far\n` +
        `    try:\n` +
        `        _far = await ed.frames[${frame}].request('layout.move', ${win}.id, [[_far_line, 0, None]], 'visual_line', 1, timeout=5)\n` +
        `    except Exception as exc:\n` +
        `        _far = repr(exc)\n` +
        `ed.start_task(_ask_far())`,
    );
    let far: unknown = null;
    for (let i = 0; i < 60 && far === null; i++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      far = await pyJson<unknown>("_far");
    }
    await settle();
    const afterFar = await view();
    const farLine = await pyJson<number>("_far_line");
    check(
      "f2.geometry_for_a_line_not_held",
      JSON.stringify(far) === `[[${farLine + 1},0,0]]` && afterFar.visible[0] === beforeFar.visible[0] && afterFar.missingVisible === 0,
      { far, farLine, held: beforeFar.held, visible: [beforeFar.visible, afterFar.visible] },
    );

    // Ten cursors moving a row at once: one request carries all of them.
    await control.eval(`${win}.show(_m); ${win}.selections.set([(n * 37 + 5, n * 37 + 5) for n in range(10)])`);
    await settle();
    await control.eval("ed.latency = []; ed.request_times = []");
    for (let i = 0; i < 60; i++) {
      press(Math.floor(i / 6) % 2 === 0 ? "Down" : "Up");
      await control.call("engine.idle", 10);
    }
    const many = await pyJson<[number[], number[]]>("[ed.latency, ed.request_times]");
    await control.eval("ed.latency = None; ed.request_times = None");
    results["gateC.ten_cursors_ms"] = { keyToFlush: stats(many[0]), roundTrip: stats(many[1]) };

    // Security: hostile text is shown as text.
    await control.eval(
      `_evil = ed.buffers.new('evil', '<img src=x onerror="window.__pwned=1">\\n<script>window.__pwned=2</script>\\n` +
        `<a href="javascript:window.__pwned=3">x</a>\\n<style>body{display:none}</style>')\n${win}.show(_evil)`,
    );
    await settle();
    const hostile = await js<{ pwned: unknown; elements: number; text: string | null; display: string }>(`(() => ({
      pwned: window.__pwned ?? null,
      elements: document.querySelectorAll('.nm-content img, .nm-content script, .nm-content a, .nm-content style').length,
      text: document.querySelector('.nm-lines [data-line="1"] .nm-content')?.textContent ?? null,
      display: getComputedStyle(document.body).display,
    }))()`);
    check(
      "security.hostile_text_is_inert",
      hostile.pwned === null && hostile.elements === 0 && hostile.display !== "none" &&
        hostile.text === '<img src=x onerror="window.__pwned=1">',
      hostile,
    );
    const sandbox = await js<{ node: boolean; require: boolean }>(
      "({ node: typeof process !== 'undefined', require: typeof require !== 'undefined' })",
    );
    check("security.no_node_in_page", !sandbox.node && !sandbox.require, sandbox);
  } catch (error) {
    failures.push(`exception: ${String(error)}`);
  }
  clearTimeout(timer);
  console.log("SMOKE-RESULT " + JSON.stringify({ ok: failures.length === 0, failures, results }));
  return failures.length === 0 ? 0 : 1;
}
