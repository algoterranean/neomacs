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

    // Gate A1: scrolling.  Count the frames in which a visible line had not arrived.
    const scrollRun = async (step: number, steps: number) => {
      let framesWithMissing = 0;
      let longest = 0;
      let run = 0;
      for (let i = 0; i < steps; i++) {
        await js<void>(`window.neomacsDebug.scrollBy(${step})`);
        await frames(1);
        const s = await view();
        if (s.missingVisible > 0) {
          framesWithMissing += 1;
          run += 1;
          longest = Math.max(longest, run);
        } else {
          run = 0;
        }
      }
      return { step, steps, framesWithMissing, longestRun: longest };
    };
    const ordinary = await scrollRun(120, 150);
    check("gateA.scroll_ordinary", ordinary.longestRun <= 2, ordinary);
    const fast = await scrollRun(1500, 60);
    results["gateA.scroll_fast"] = fast; // reported, not a pass or fail
    await settle();
    v = await view();
    check("gateA.scroll_settles", v.missingVisible === 0 && v.visible[0] > 100, v.visible);

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
