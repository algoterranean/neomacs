// A scripted run of the real app against a real engine: the checks for F0, F1 and Gate A
// (doc/IMPLEMENTATION.md).  Prints one line, `SMOKE-RESULT {json}`, and exits 0 or 1.

import { type BrowserWindow, clipboard } from "electron";
import type { Shell } from "./main.ts";
import { mkdtempSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";
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
  shell: Shell,
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
  // Keys pressed through Chromium take a while to reach the page.  `settle`
  // first waits until the page has sent as many as were pressed.
  let keysPressed = -1; // -1 until keys are first pressed
  const settle = async () => {
    if (keysPressed >= 0) {
      for (let i = 0; i < 300; i++) {
        const sent = await js<number>("window.neomacsDebug.snapshot().input.keys");
        if (sent >= keysPressed) break;
        await frames(1);
      }
    }
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
  }, Number(process.env["NEOMACS_SMOKE_TIMEOUT"] ?? "300") * 1000);

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
    keysPressed = await js<number>("window.neomacsDebug.snapshot().input.keys");
    const press = (keyCode: string, modifiers: ("shift" | "control" | "alt" | "meta")[] = []) => {
      keysPressed += 1;
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

    // Step 7: redefine a built-in command and rebind a key from the scratch
    // buffer; both take effect on the next keystroke.
    results["engine.reevaluated"] = await pyJson<number>("ed.reevaluated");
    await control.eval(
      `_sc = ed.buffers.get('*scratch*')\n` +
        `_sc.replace(0, len(_sc), "@command\\ndef end_of_line(sel: Selection) -> None:\\n    sel.start_of_line()\\n\\nglobal_map.bind('<f6>', 'end_of_line')\\n")\n` +
        `${win}.show(_sc)`,
    );
    await settle();
    press("c", ["control"]);
    press("b", ["control"]);
    await settle();
    u = await ui();
    const evaluated = u.chrome.message;
    await control.eval(`${win}.show(_k); ${win}.selections.set([(5, 5)])`);
    await settle();
    press("End");
    await settle();
    const afterEnd = (await ui()).selections;
    await control.eval(`${win}.selections.set([(5, 5)])`);
    press("F6");
    await settle();
    const afterF6 = (await ui()).selections;
    check(
      "live.redefinition_and_binding_take_effect_on_the_next_key",
      evaluated === "Evaluated" && JSON.stringify(afterEnd) === "[[1,0,1,0]]" && JSON.stringify(afterF6) === "[[1,0,1,0]]",
      { evaluated, afterEnd, afterF6 },
    );
    press("x", ["alt"]);
    await settle();
    type("find_source");
    press("Return");
    await settle();
    type("end_of_line");
    press("Return");
    await settle();
    const sourceIn = await pyText(`${win}.buffer.name`);
    // Put the real one back by evaluating its file, found through the same command.
    await control.eval(`import neomacs.std.movement as _mvmod\n${win}.show(ed.buffers.visit(_mvmod.__file__))`);
    await settle();
    press("c", ["control"]);
    press("b", ["control"]);
    await settle();
    await control.eval(`global_map.unbind('<f6>'); ${win}.show(_k); ${win}.selections.set([(5, 5)])`);
    await settle();
    press("End");
    await settle();
    const restored = (await ui()).selections;
    check(
      "live.find_source_and_restore_from_the_file",
      sourceIn === "'*scratch*'" && JSON.stringify(restored) === "[[1,11,1,11]]" && (await pyText("ed.eval_warnings")) === "[]",
      { sourceIn, restored, warnings: await pyText("ed.eval_warnings") },
    );

    // Step 8: a breakpoint in a command; the window keeps working while it is paused.
    await control.eval(
      `_sc.replace(0, len(_sc), "@command\\ndef dbg_demo() -> None:\\n    first = 1\\n    second = first + 1\\n    ed.message(f'demo {second}')\\n")\n` +
        `${win}.show(_sc)`,
    );
    await settle();
    press("c", ["control"]);
    press("b", ["control"]);
    await settle();
    await control.eval(
      `from neomacs.debug import breakpoints as _bp\n_bp.toggle(ed.debugger, _sc, 3)\nglobal_map.bind('<f7>', 'dbg_demo')`,
    );
    await settle();
    const markShown = (await js<string>("document.body.innerText")).includes("●");
    await control.eval(`${win}.show(_k)`);
    await settle();
    press("F7");
    await settle();
    const pausedAt = await pyText(`(ed.debugger.status, ed.debugger.stack[0][1], ${win}.buffer.name)`);
    const pausedPage = await js<string>("document.body.innerText");
    check(
      "debug.pauses_and_shows_it",
      pausedAt === "('paused', 3, '*scratch*')" && pausedPage.includes("[paused]") && markShown && pausedPage.includes("▶"),
      { pausedAt, modeline: pausedPage.includes("[paused]"), breakpointMark: markShown, lineMark: pausedPage.includes("▶") },
    );
    press("F10");
    await settle();
    const steppedTo = await pyText("(ed.debugger.status, ed.debugger.stack[0][1])");
    // Other commands run while it is paused: a prompt, and an assignment to a local.
    press(":", ["alt"]);
    await settle();
    const promptWhilePaused = (await ui()).chrome.prompt;
    type("first = 10");
    press("Return");
    await settle();
    press("F10");
    await settle();
    const secondIs = await pyText("ed.debugger.stack[0][0].f_locals.get('second')");
    press("F5");
    await settle();
    u = await ui();
    const resumedPage = await js<string>("document.body.innerText");
    check(
      "debug.step_change_a_local_continue",
      steppedTo === "('paused', 4)" && promptWhilePaused === "Eval: " && secondIs === "11" && u.chrome.message === "demo 11" &&
        !resumedPage.includes("[paused]") && (await pyText(`(ed.debugger.status, ${win}.buffer.name)`)) === "('', 'keys')",
      { steppedTo, promptWhilePaused, secondIs, message: u.chrome.message, stillShown: resumedPage.includes("[paused]") },
    );
    await control.eval(`_bp.toggle(ed.debugger, _sc, 3); global_map.unbind('<f7>'); ${win}.selections.set([(5, 5)])`);
    await settle();

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
    check("f2.prompt_opens", u.chrome.prompt === "M-x " && (u.chrome.popup?.length ?? 0) > 10 && u.focus !== u.mainView, { chrome: u.chrome, focus: u.focus, mainView: u.mainView, engine: await pyText("[(m.label, m.closed) for m in ed.frames[" + frame + "].prompts]") });
    type("goto li"); // two parts, matched in any order: only goto_line has both
    await settle();
    u = await ui();
    check("f2.completion_narrows", JSON.stringify(u.chrome.popup) === '["goto_line"]' && u.chrome.promptText === "goto li", [u.chrome.popup, u.chrome.promptText]);
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
    // Gate C is about the reference machine: there the 99th percentile is held
    // to the engine's 5 ms.  A shared CI runner (NEOMACS_BUDGET_SCALE above 1)
    // has stalls of tens of milliseconds that are none of the editor's doing,
    // so there only the median is judged.
    const onReference = (Number(process.env["NEOMACS_BUDGET_SCALE"] ?? "1") || 1) <= 1;
    check(
      "gateC.vertical_motion_within_budget",
      keyToFlush.n >= 200 && roundTrip.n >= 200 && (onReference ? keyToFlush.p99 < 5 : keyToFlush.p50 < 5),
      { onReference, ...keyToFlush },
    );

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

    // ---------------------------------------------------------------------------
    // Step 10 (F3, F4): the whole interface.
    interface Shown { id: number; name: string; rect: number[]; focused: boolean; modeline: string; inPanel: string | null; inline: boolean; text: string }
    interface PanelShown { id: number; name: string; floating: boolean; rect: number[]; tabs: string[]; active: string | null }
    interface Arrangement { views: Shown[]; panels: PanelShown[]; dividers: number[][]; workspace: number[] }
    const arrangement = () => js<Arrangement>("window.neomacsDebug.arrangement()");
    const fr = `ed.frames[${frame}]`;
    const mouseAt = async (x: number, y: number, clicks = 1, modifiers: ("shift" | "control" | "alt" | "meta")[] = []) => {
      contents.sendInputEvent({ type: "mouseDown", x: Math.round(x), y: Math.round(y), button: "left", clickCount: clicks, modifiers });
      contents.sendInputEvent({ type: "mouseUp", x: Math.round(x), y: Math.round(y), button: "left", clickCount: clicks, modifiers });
      await frames(4);
      await settle();
    };
    const dragFrom = async (x0: number, y0: number, x1: number, y1: number) => {
      contents.sendInputEvent({ type: "mouseDown", x: Math.round(x0), y: Math.round(y0), button: "left", clickCount: 1 });
      for (let i = 1; i <= 6; i++) {
        const t = i / 6;
        contents.sendInputEvent({ type: "mouseMove", x: Math.round(x0 + (x1 - x0) * t), y: Math.round(y0 + (y1 - y0) * t), button: "left", modifiers: ["leftbuttondown"] });
        await frames(2);
      }
      contents.sendInputEvent({ type: "mouseUp", x: Math.round(x1), y: Math.round(y1), button: "left", clickCount: 1 });
      await frames(4);
      await settle();
    };
    const pointAt = (view: number, line: number, offset: number) =>
      js<{ x: number; y: number; width: number } | null>(`window.neomacsDebug.pointAt(${view}, ${line}, ${offset})`);
    const centre = (rect: number[]) => [(rect[0] as number) + (rect[2] as number) / 2, (rect[1] as number) + (rect[3] as number) / 2] as [number, number];
    // With NEOMACS_SMOKE_SHOTS set to a directory, pictures of the page are saved there to be looked at.
    const shotDir = process.env["NEOMACS_SMOKE_SHOTS"];
    const shot = async (name: string) => {
      if (!shotDir) return;
      await frames(4);
      writeFileSync(path.join(shotDir, `${name}.png`), (await contents.capturePage()).toPNG());
    };
    check("f3.claims_panels", (await pyText(`${fr}.draws_panels`)) === "True");

    // -- the debugger's layout: source, a bottom panel, breakpoints in the gutter --
    await control.eval(
      `_sc.replace(0, len(_sc), "@command\\ndef dbg_demo() -> None:\\n    first = 1\\n    second = first + 1\\n    ed.message(f'demo {second}')\\n")\n${win}.show(_sc)`,
    );
    await settle();
    press("c", ["control"]);
    press("b", ["control"]);
    await settle();
    await control.eval(`_bp.toggle(ed.debugger, _sc, 3); global_map.bind('<f7>', 'dbg_demo'); ${win}.show(_k)`);
    await settle();
    press("F7");
    await settle();
    let shown = await arrangement();
    const source = shown.views.find((v) => v.name === "*scratch*");
    const stack = shown.views.find((v) => v.name === "*debug*");
    const bottom = shown.panels.find((p) => p.name === "bottom");
    const gutterMarks = await js<string[]>(
      "[...document.querySelectorAll('.nm-window .nm-gutter-col[data-column=\"signs\"]')].map((c) => c.textContent).filter((t) => t)",
    );
    await shot("1-debugger");
    check(
      "f3.debugger_layout",
      source !== undefined && stack !== undefined && bottom !== undefined && source.inPanel === null && stack.inPanel === "bottom" &&
        (bottom.tabs.includes("*debug*")) && stack.text.includes("Stack, innermost first") && stack.text.includes("dbg_demo") &&
        (source.rect[1] as number) + (source.rect[3] as number) <= (bottom.rect[1] as number) + 1 && (bottom.rect[3] as number) > 80 &&
        source.modeline.includes("[paused]") && stack.modeline === "" && gutterMarks.includes("▶") && source.focused,
      { source: source && { rect: source.rect, modeline: source.modeline, focused: source.focused }, bottom, gutterMarks, stackText: stack?.text.slice(0, 60) },
    );
    press("F10");
    await settle();
    shown = await arrangement();
    check("f3.debugger_panel_follows", (shown.views.find((v) => v.name === "*debug*")?.text ?? "").includes("first = 1"), shown.views.find((v) => v.name === "*debug*")?.text.slice(0, 200));
    press("F5");
    await settle();
    await control.eval(`_bp.toggle(ed.debugger, _sc, 3); global_map.unbind('<f7>'); ${fr}.panels.pop('bottom', None); ${win}.show(_k)`);
    await settle();
    shown = await arrangement();
    check("f3.panel_goes_when_the_engine_says", shown.panels.length === 0 && shown.views.length === 1, shown.panels);

    // -- the mouse: click, double click, drag, the gutter --
    await control.eval(`_k.replace(0, len(_k), 'alpha beta gamma\\nsecond line here\\nthird'); ${win}.selections.set([(0, 0)])`);
    await settle();
    const mainId = (await ui()).mainView as number;
    let at = await pointAt(mainId, 2, 7);
    await mouseAt((at?.x ?? 0) + 1, at?.y ?? 0);
    u = await ui();
    check("f4.click_puts_the_cursor", at !== null && JSON.stringify(u.selections) === "[[2,7,2,7]]", { at, selections: u.selections });
    at = await pointAt(mainId, 1, 7);
    await mouseAt((at?.x ?? 0) + 1, at?.y ?? 0, 2);
    check("f4.double_click_selects_the_word", (await pyText(`${win}.selections.primary.text`)) === "'beta'", await pyText(`${win}.selections.primary.text`));
    const from = await pointAt(mainId, 1, 0);
    const to = await pointAt(mainId, 2, 6);
    await dragFrom((from?.x ?? 0), from?.y ?? 0, (to?.x ?? 0), to?.y ?? 0);
    const dragged = await pyText(`${win}.selections.primary.text`);
    check("f4.drag_selects", dragged === "'alpha beta gamma\\nsecond'", dragged);
    // A quick drag: the button comes up before the frame in which the movement would have been sent.
    const q0 = await pointAt(mainId, 3, 0);
    const q1 = await pointAt(mainId, 3, 5);
    contents.sendInputEvent({ type: "mouseDown", x: Math.round(q0?.x ?? 0), y: Math.round(q0?.y ?? 0), button: "left", clickCount: 1 });
    contents.sendInputEvent({ type: "mouseMove", x: Math.round((q1?.x ?? 0) + 2), y: Math.round(q1?.y ?? 0), button: "left", modifiers: ["leftbuttondown"] });
    contents.sendInputEvent({ type: "mouseUp", x: Math.round((q1?.x ?? 0) + 2), y: Math.round(q1?.y ?? 0), button: "left", clickCount: 1 });
    await frames(6);
    await settle();
    const quick = await pyText(`${win}.selections.primary.text`);
    check("f4.a_quick_drag_selects_too", quick === "'third'", quick);
    const gutterBox = await js<number[]>(
      "(() => { const r = document.querySelector('.nm-window .nm-line[data-line=\"3\"] .nm-gutter-col').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()",
    );
    await mouseAt(gutterBox[0] as number, gutterBox[1] as number);
    const afterGutter = (await ui()).chrome.message;
    check("f4.gutter_click", afterGutter.startsWith("Breakpoint at line 3"), afterGutter);
    await mouseAt(gutterBox[0] as number, gutterBox[1] as number);
    const mouseSent = (await js<{ mouse: { sent: number } }>("window.neomacsDebug.snapshot()")).mouse.sent;
    check("f4.mouse_events_sent", mouseSent >= 8, mouseSent);

    // -- search: a box of its own over the corner of the window, not the message strip --
    await control.eval(`_k.replace(0, len(_k), 'alpha beta gamma\\nsecond line here\\nthird beta'); ${win}.selections.set([(0, 0)])`);
    await settle();
    press("s", ["control"]);
    await settle();
    type("beta");
    await settle();
    interface SearchBox { label: string; text: string; rect: number[] }
    const searching = (await js<{ chrome: { search: SearchBox | null; prompt: string | null; keys: string } }>("window.neomacsDebug.snapshot()")).chrome;
    await shot("7-search");
    const wsNow = (await arrangement()).workspace;
    check(
      "f4.search_box",
      searching.search !== null && searching.search.label === "Search [1/2]: " && searching.search.text === "beta" && searching.prompt === null &&
        searching.keys === "" && (searching.search.rect[0] as number) > (wsNow[2] as number) / 2 && (searching.search.rect[1] as number) < 40 &&
        (await pyText(`${win}.selections.primary.text`)) === "'beta'" && (await ui()).focus !== mainId,
      { searching, selected: await pyText(`${win}.selections.primary.text`) },
    );
    press("s", ["control"]);
    await settle();
    const second2 = (await js<{ chrome: { search: SearchBox | null } }>("window.neomacsDebug.snapshot()")).chrome.search;
    press("Return");
    await settle();
    const afterSearch = (await js<{ chrome: { search: SearchBox | null } }>("window.neomacsDebug.snapshot()")).chrome.search;
    check(
      "f4.search_box_closes",
      second2?.label === "Search [2/2]: " && afterSearch === null && (await ui()).focus === mainId &&
        (await pyText(`${win}.buffer.line_of(${win}.cursor.pos)`)) === "3",
      { second2, afterSearch },
    );

    // -- splits: two windows, a divider that drags, a click that changes focus --
    press("x", ["control"]);
    press("2");
    await settle();
    shown = await arrangement();
    const twoWindows = shown.views.filter((v) => v.name === "keys");
    check(
      "f3.split_shows_two_windows",
      twoWindows.length === 2 && shown.dividers.length === 1 && twoWindows.every((v) => v.modeline.includes("keys")) &&
        twoWindows.filter((v) => v.focused).length === 1 && Math.abs((twoWindows[0]?.rect[3] ?? 0) - (twoWindows[1]?.rect[3] ?? 99)) <= 8,
      { views: twoWindows.map((v) => ({ id: v.id, rect: v.rect, focused: v.focused })), dividers: shown.dividers },
    );
    await shot("2-split");
    const bar = shown.dividers[0] as number[];
    const [bx, by] = centre(bar);
    await dragFrom(bx, by, bx, by - 120);
    const ratios = await pyJson<number[]>(`${fr}.root.ratios`);
    shown = await arrangement();
    const resized = shown.views.filter((v) => v.name === "keys").sort((a, b) => (a.rect[1] as number) - (b.rect[1] as number));
    check(
      "f3.divider_drag_resizes",
      ratios.length === 2 && (ratios[0] as number) < 0.4 && (resized[0]?.rect[3] ?? 999) + 60 < (resized[1]?.rect[3] ?? 0),
      { ratios, heights: resized.map((v) => v.rect[3]) },
    );
    const unfocused = resized.find((v) => !v.focused);
    const [ux, uy] = centre(unfocused?.rect ?? [0, 0, 0, 0]);
    await mouseAt(ux, uy - 10);
    const focusNow = (await ui()).focus;
    check("f3.click_focuses_the_other_window", unfocused !== undefined && focusNow === unfocused.id, { focusNow, wanted: unfocused?.id });
    press("x", ["control"]);
    press("1");
    await settle();
    check("f3.one_window_again", (await arrangement()).views.length === 1);

    // -- panels: a docked one with tabs, acted on with the mouse --
    await control.eval(
      `_p1 = ed.buffers.new('*one*', 'first tab'); _p2 = ed.buffers.new('*two*', 'second tab')\n` +
        `from neomacs import panels as _pn\n_v1 = _pn.place(${fr}, _p1, 'help', focus=False); _v2 = _pn.place(${fr}, _p2, 'help', focus=False)`,
    );
    await settle();
    shown = await arrangement();
    let side = shown.panels.find((p) => p.name === "side");
    const mainRect = shown.views.find((v) => v.name === "keys")?.rect ?? [0, 0, 0, 0];
    check(
      "f3.docked_panel_with_tabs",
      side !== undefined && !side.floating && JSON.stringify(side.tabs) === '["*one*","*two*"]' && side.active === "*two*" &&
        (side.rect[0] as number) >= (mainRect[0] as number) + (mainRect[2] as number) - 2 &&
        (shown.views.find((v) => v.name === "*two*")?.text ?? "") === "second tab",
      { side, mainRect },
    );
    await shot("3-side-panel");
    const rebuildsBefore = (await js<{ layoutRebuilds: number }>("window.neomacsDebug.snapshot()")).layoutRebuilds;
    const tabPoint = await js<number[]>(
      "(() => { const r = [...document.querySelectorAll('.nm-tab')].find((t) => t.textContent.includes('*one*')).getBoundingClientRect(); return [r.left + 12, r.top + r.height / 2]; })()",
    );
    await mouseAt(tabPoint[0] as number, tabPoint[1] as number);
    shown = await arrangement();
    side = shown.panels.find((p) => p.name === "side");
    check(
      "f3.tab_click_asks_the_engine",
      side?.active === "*one*" && (await pyText(`${fr}.panels['side'].active.buffer.name`)) === "'*one*'" &&
        (await js<{ layoutRebuilds: number }>("window.neomacsDebug.snapshot()")).layoutRebuilds > rebuildsBefore,
      { active: side?.active },
    );
    const floatPoint = await js<number[]>(
      "(() => { const r = document.querySelector('.nm-tab-tool[data-action=\"float\"]').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()",
    );
    await mouseAt(floatPoint[0] as number, floatPoint[1] as number);
    shown = await arrangement();
    side = shown.panels.find((p) => p.name === "side");
    const ws = shown.workspace;
    await shot("4-floating-panel");
    check(
      "f3.floating_panel",
      side !== undefined && side.floating && (await pyText(`${fr}.panels['side'].mode`)) === "'floating'" &&
        (side.rect[0] as number) > (ws[0] as number) + 40 && (side.rect[0] as number) + (side.rect[2] as number) < (ws[0] as number) + (ws[2] as number) - 40 &&
        (shown.views.find((v) => v.name === "keys")?.rect[2] ?? 0) > (ws[2] as number) - 4,
      { side, workspace: ws },
    );
    // Drag its tab strip to the bottom edge: it docks there.
    const stripPoint = await js<number[]>(
      "(() => { const r = document.querySelector('.nm-floating .nm-tabs').getBoundingClientRect(); return [r.left + r.width * 0.6, r.top + r.height / 2]; })()",
    );
    await dragFrom(stripPoint[0] as number, stripPoint[1] as number, (ws[0] as number) + (ws[2] as number) / 2, (ws[1] as number) + (ws[3] as number) - 12);
    shown = await arrangement();
    side = shown.panels.find((p) => p.name === "side");
    check(
      "f3.drag_docks_at_a_side",
      side !== undefined && !side.floating && (await pyText(`(${fr}.panels['side'].mode, ${fr}.panels['side'].side)`)) === "('docked', 'bottom')" &&
        (side.rect[1] as number) > (ws[1] as number) + (ws[3] as number) / 2,
      { side },
    );
    const closePoint = await js<number[]>(
      "(() => { const r = document.querySelector('.nm-tab-active .nm-tab-close').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()",
    );
    await mouseAt(closePoint[0] as number, closePoint[1] as number);
    shown = await arrangement();
    check("f3.close_button_closes_a_tab", JSON.stringify(shown.panels.find((p) => p.name === "side")?.tabs) === '["*two*"]', shown.panels);
    await control.eval(`${fr}.panels.pop('side'); ed.buffers.kill(_p1); ed.buffers.kill(_p2)`);
    await settle();

    // -- margins, a maximum width, paragraph attributes, an embedded minibuffer --
    const plainLeft = await js<number>("document.querySelector('.nm-window .nm-content').getBoundingClientRect().left");
    await control.eval(
      `_k.replace(0, len(_k), 'Title\\nbody')\n_k.locals['margins'] = {'left': 4, 'right': 2}; _k.locals['max_width'] = 30\n` +
        `_al = _k.layer('para').add(0, 5, line={'align': 'center'}); _k.invalidate(0, len(_k))`,
    );
    await settle();
    const laid = await js<{ left: number; width: number; align: string; em: number }>(
      "(() => { const c = document.querySelector('.nm-window .nm-line[data-line=\"1\"] .nm-content'); const r = c.getBoundingClientRect(); return { left: r.left, width: r.width, align: getComputedStyle(c).textAlign, em: parseFloat(getComputedStyle(document.querySelector('.nm-frame')).fontSize) }; })()",
    );
    check(
      "f3.margins_max_width_and_alignment",
      laid.left > plainLeft + 3 * laid.em && Math.abs(laid.width - 30 * laid.em) < 2 && laid.align === "center",
      { plainLeft, ...laid },
    );
    await control.eval(`_al.remove(); _k.locals.pop('margins'); _k.locals.pop('max_width'); _k.invalidate(0, len(_k))`);
    await settle();
    await control.eval(
      `from neomacs.minibuffer import Minibuffer\n_k.replace(0, len(_k), 'Name: [] end')\n` +
        `_mb = Minibuffer(${fr}, placeholder='type here'); ${win}.embed(_mb, 7); ${fr}.focus_view(_mb.view)`,
    );
    await settle();
    type("hi");
    await settle();
    shown = await arrangement();
    await shot("6-inline-minibuffer");
    const embedded = shown.views.find((v) => v.inline);
    const host = shown.views.find((v) => v.name === "keys");
    check(
      "f3.inline_minibuffer",
      embedded !== undefined && embedded.text === "hi" && (await pyText("_mb.text")) === "'hi'" && host !== undefined &&
        (embedded.rect[0] as number) > (host.rect[0] as number) && (embedded.rect[1] as number) >= (host.rect[1] as number) - 1 &&
        (embedded.rect[3] as number) < 60,
      { embedded, hostRect: host?.rect },
    );
    await control.eval(`_mb.close(); ${fr}.focus_view(${win})`);
    await settle();
    check("f3.inline_minibuffer_goes", (await arrangement()).views.every((v) => !v.inline));

    // -- images, through the private resource scheme --
    // A real picture: 8 by 8, solid orange.
    const makePng = (size: number, rgb: [number, number, number]) => {
      const chunk = (type: string, data: Buffer) => {
        const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
        const length = Buffer.alloc(4);
        length.writeUInt32BE(data.length);
        const crc = Buffer.alloc(4);
        crc.writeUInt32BE(zlib.crc32(body) >>> 0);
        return Buffer.concat([length, body, crc]);
      };
      const header = Buffer.alloc(13);
      header.writeUInt32BE(size, 0);
      header.writeUInt32BE(size, 4);
      header.set([8, 2, 0, 0, 0], 8); // 8 bits a channel, RGB
      const row = Buffer.concat([Buffer.from([0]), Buffer.from(new Array<number[]>(size).fill(rgb).flat())]);
      const pixels = zlib.deflateSync(Buffer.concat(new Array<Buffer>(size).fill(row)));
      return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", header), chunk("IDAT", pixels), chunk("IEND", Buffer.alloc(0))]);
    };
    const png = makePng(8, [255, 140, 0]);
    const pngPath = path.join(os.tmpdir(), `neomacs-smoke-${process.pid}.png`);
    writeFileSync(pngPath, png);
    const secretPath = path.join(os.tmpdir(), `neomacs-smoke-secret-${process.pid}.png`);
    writeFileSync(secretPath, png);
    await control.eval(
      `_k.replace(0, len(_k), 'see X and Y here')\n_img = ed.resources.image(${JSON.stringify(pngPath)})\n` +
        `_raw = ed.resources.image(data=__import__('base64').b64decode(${JSON.stringify(png.toString("base64"))}), mime='image/png')\n` +
        `_s1 = _k.layer('img').add(4, 5, display={'image': _img, 'width': 24, 'height': 24, 'alt': 'dot'})\n` +
        `_s2 = _k.layer('img').add(10, 11, display={'image': _raw, 'alt': 'raw'})`,
    );
    await settle();
    await frames(6);
    await shot("5-images");
    const images = await js<{ src: string; complete: boolean; naturalWidth: number; width: number; alt: string }[]>("window.neomacsDebug.images()");
    check(
      "f4.images_show",
      images.length === 2 && images.every((i) => i.complete && i.naturalWidth === 8) && (images[0]?.src ?? "").startsWith(`neomacs-resource://frame-${frame}/`) &&
        (images[1]?.src ?? "").startsWith("blob:") && images[0]?.width === 24 && images[0]?.alt === "dot",
      images,
    );
    // Only what the engine defined is served: not another id, and not a path of the page's choosing.
    const refused = await js<string[]>(`Promise.all([
      "neomacs-resource://frame-${frame}/9999",
      "neomacs-resource://frame-${frame}/" + encodeURIComponent(${JSON.stringify(secretPath)}),
      "neomacs-resource://frame-${frame + 1000}/1",
      "file://" + ${JSON.stringify(secretPath)},
    ].map((src) => new Promise((resolve) => { const i = new Image(); i.onload = () => resolve("loaded"); i.onerror = () => resolve("refused"); i.src = src; })))`);
    check("f4.only_defined_resources_are_served", refused.every((r) => r === "refused"), refused);
    await control.eval("_s1.remove(); _s2.remove()");
    await settle();

    // An image with no stated size is taller than its line was when the line was
    // measured.  When it arrives the view lays out again by itself: nothing else
    // happens here to cause a render.
    const tallPath = path.join(os.tmpdir(), `neomacs-smoke-tall-${process.pid}.png`);
    writeFileSync(tallPath, makePng(60, [0, 160, 255]));
    await control.eval(
      `_k.replace(0, len(_k), 'X tall\\nbelow it'); ${win}.selections.set([(len(_k), len(_k))])\n` +
        `_tall = ed.resources.image(${JSON.stringify(tallPath)}); _s3 = _k.layer('img').add(0, 1, display={'image': _tall, 'alt': 'tall'})`,
    );
    await control.call("engine.idle", 10);
    let late: { loaded: boolean; lineTop: number; cursorTop: number; lineHeight: number } = { loaded: false, lineTop: 0, cursorTop: -1, lineHeight: 0 };
    for (let i = 0; i < 100; i++) {
      await new Promise((resolve) => setTimeout(resolve, 30));
      late = await js(`(() => {
        const img = document.querySelector('img.nm-image');
        const line = document.querySelector('.nm-window .nm-line[data-line="2"]');
        const cursor = document.querySelector('.nm-window .nm-primary');
        return {
          loaded: !!img && img.complete && img.naturalWidth === 60,
          lineTop: line ? line.getBoundingClientRect().top : -1,
          cursorTop: cursor ? cursor.getBoundingClientRect().top : -2,
          lineHeight: document.querySelector('.nm-window .nm-line[data-line="1"]').getBoundingClientRect().height,
        };
      })()`);
      if (late.loaded && Math.abs(late.lineTop - late.cursorTop) < 3) break;
    }
    check("f4.a_late_image_is_laid_out_again", late.loaded && late.lineHeight >= 60 && Math.abs(late.lineTop - late.cursorTop) < 3, late);
    await control.eval("_s3.remove()");
    await settle();

    // -- the clipboard, through the main process --
    const savedClipboard = await clipboard.readText();
    try {
      await control.eval(`_k.replace(0, len(_k), 'copy this text'); ${win}.selections.set([(5, 9)])`);
      await settle();
      press("w", ["alt"]);
      await settle();
      await frames(4);
      const copied = await clipboard.readText();
      clipboard.writeText("from outside");
      await control.eval(`${win}.selections.set([(len(_k), len(_k))])`);
      await settle();
      press("y", ["control"]);
      await settle();
      check("f4.clipboard_both_ways", copied === "this" && (await pyText("_k.text")) === "'copy this textfrom outside'", { copied, text: await pyText("_k.text") });

      // -- the application menu, built from the engine's `menu` event --
      const menu = shell.appMenu();
      const labels = (menu?.items ?? []).map((item) => item.label);
      const find = (top: string, label: string) => menu?.items.find((i) => i.label === top)?.submenu?.items.find((i) => i.label === label);
      const selectAll = find("Edit", "Select All");
      const open = find("File", "Open…");
      selectAll?.click();
      await frames(4);
      await settle();
      const selectedAll = await pyText(`${win}.selections.primary.text`);
      clipboard.writeText(" MENU");
      await control.eval(`${win}.selections.set([(len(_k), len(_k))])`);
      await settle();
      find("Edit", "Paste")?.click();
      await frames(6);
      await settle();
      check(
        "f4.application_menu",
        ["File", "Edit", "Tools", "View", "Help"].every((l) => labels.includes(l)) && selectAll !== undefined &&
          selectedAll === "'copy this textfrom outside'" && (await pyText("_k.text")).endsWith(" MENU'") &&
          (process.platform !== "darwin" || (labels[0] !== "File" && open?.accelerator === "Cmd+O")),
        { labels, selectedAll, text: await pyText("_k.text"), openAccelerator: open?.accelerator },
      );
    } finally {
      clipboard.writeText(savedClipboard);
    }

    // -- a second window on the same buffer --
    await control.eval(`_k.replace(0, len(_k), 'shared text'); ${win}.selections.set([(0, 0)])`);
    await settle();
    const windowsBefore = shell.windows.size;
    press("x", ["control"]);
    press("5");
    press("2");
    await settle();
    let second: { window: BrowserWindow; frame: number | null } | undefined;
    for (let i = 0; i < 100 && (second === undefined || second.frame === null); i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      second = [...shell.windows.values()].find((w) => w.window !== state.window);
    }
    const js2 = <T>(code: string) => (second as { window: BrowserWindow }).window.webContents.executeJavaScript(code, true) as Promise<T>;
    const secondFrame = second?.frame ?? -1;
    await control.eval(`ed.frames[${secondFrame}].window.show(_k)`);
    await control.call("engine.idle", 10);
    await js2<void>("window.neomacsDebug.frames(4)");
    type("typed in the first, ");
    await settle();
    await js2<void>("window.neomacsDebug.frames(4)");
    const inSecond = await js2<string>("document.querySelector('.nm-window .nm-content').textContent");
    check(
      "f4.second_window_same_buffer",
      shell.windows.size === windowsBefore + 1 && secondFrame > 0 && secondFrame !== frame &&
        inSecond === "typed in the first, shared text" && (await pyText("len(ed.frames)")) === "2",
      { windows: shell.windows.size, secondFrame, inSecond },
    );
    second?.window.destroy();
    for (let i = 0; i < 60 && (await pyText("len(ed.frames)")) !== "1"; i++) await new Promise((resolve) => setTimeout(resolve, 50));
    check("f4.closing_a_window_removes_its_frame", (await pyText("len(ed.frames)")) === "1" && shell.windows.size === windowsBefore);

    // -- step 12, F5: themes, labels, and the performance budgets --
    {
      await control.eval(
        `_sel12 = ${win}.selections.ranges()\n` +
          `ed.faces.own.pop('default', None)\n` + // the tests above gave the default face colours of their own
          `ed.options.theme = 'dark'\n` +
          `_perf = ed.buffers.new('perf.py')\n_perf.set_mode('python')\n` +
          `_perf.insert(0, ''.join(f'def function_{i}(a, b):\\n    return a + b * {i}  # the {i}th\\n\\n' for i in range(120)))\n` +
          `${win}.show(_perf)\n${win}.selections.set([(0, 0)])\n${win}.reveal(0, 'top')`,
      );
      await settle();
      const look = () =>
        js<{ bg: string; fg: string; surface: string; scheme: string; keyword: string }>(
          `(() => { const f = getComputedStyle(document.querySelector('.nm-frame')); const r = getComputedStyle(document.documentElement);
            const k = [...document.querySelectorAll('.nm-content span')].find((e) => e.textContent === 'def');
            return { bg: f.backgroundColor, fg: f.color, surface: r.getPropertyValue('--nm-surface').trim(), scheme: r.getPropertyValue('color-scheme').trim(), keyword: k ? getComputedStyle(k).color : '' }; })()`,
        );
      const dark = await look();
      const before12 = await js<{ resets: number; layoutRebuilds: number }>("window.neomacsDebug.snapshot()");
      await control.eval("ed.options.theme = 'light'");
      await settle();
      const light = await look();
      const after12 = await js<{ resets: number; layoutRebuilds: number }>("window.neomacsDebug.snapshot()");
      check(
        "f5.theme_changes_text_and_chrome_without_redrawing",
        dark.bg === "rgb(30, 30, 30)" && dark.surface === "#252526" && dark.scheme === "dark" && dark.keyword === "rgb(197, 134, 192)" &&
          light.bg === "rgb(255, 255, 255)" && light.fg === "rgb(31, 35, 40)" && light.surface === "#f6f8fa" && light.scheme === "light" &&
          light.keyword === "rgb(166, 38, 164)" && after12.resets === before12.resets && after12.layoutRebuilds === before12.layoutRebuilds,
        { dark, light, before12, after12 },
      );
      await shot("12-light-theme");
      await control.eval("ed.options.theme = 'dark'");
      await settle();
      check("f5.theme_goes_back", (await look()).bg === "rgb(30, 30, 30)", await look());

      // Labels on views and regions; messages announced.
      await control.eval("ed.message('something went wrong', kind='error')");
      await settle();
      press("x", ["alt"]);
      await settle();
      const labels = await js<Record<string, unknown>>(
        `(() => { const a = (sel, name) => document.querySelector(sel)?.getAttribute(name) ?? null;
          return { root: [a('#root', 'role'), a('#root', 'aria-label')],
            view: [a('.nm-window .nm-view', 'role'), a('.nm-window .nm-view', 'aria-label')],
            modeline: a('.nm-window .nm-modeline', 'aria-label'),
            message: [a('.nm-message', 'role'), a('.nm-message', 'aria-live'), document.querySelector('.nm-message')?.textContent],
            toast: [...document.querySelectorAll('.nm-toast-error')].map((t) => t.getAttribute('role')).slice(-1),
            palette: [a('.nm-palette', 'role'), a('.nm-palette', 'aria-label')],
            popup: [a('.nm-popup', 'role'), document.querySelectorAll('.nm-popup-item[role=option]').length > 5,
              document.querySelectorAll('.nm-popup-item[aria-selected=true]').length],
            prompt: a('.nm-palette .nm-view', 'aria-label') }; })()`,
      );
      press("g", ["control"]);
      await settle();
      check(
        "f5.views_and_regions_are_labelled_and_messages_announced",
        JSON.stringify(labels) ===
          JSON.stringify({
            root: ["application", "Neomacs"],
            view: ["group", "Buffer perf.py"],
            modeline: "Status line",
            message: ["status", "polite", "something went wrong"],
            toast: ["alert"],
            palette: ["dialog", "M-x"],
            popup: ["listbox", true, 1],
            prompt: "Input",
          }),
        labels,
      );

      // The budgets of FRONTEND 11.  A shared CI machine is allowed a multiple.
      const scale = Number(process.env["NEOMACS_BUDGET_SCALE"] ?? "1") || 1;
      await js<void>("window.neomacsDebug.perfReset()");
      for (const ch of "value = function_3(1, 2) + function_4(3, 4)  # typed one key at a time, as a person would") {
        type(ch);
        await settle();
      }
      for (let i = 0; i < 40; i++) {
        press(i < 20 ? "Down" : "Up"); // each is one `layout.move` for one position
        await settle();
      }
      await js<unknown>("window.neomacsDebug.scrollRun(40, 60)");
      await settle();
      interface PerfReport { summary: Record<string, { n: number; p50: number; p90: number; max: number }>; over: Record<string, [number, number]> }
      const report = await js<PerfReport>(`window.neomacsDebug.perf(${scale})`);
      const counted = (name: string) => report.summary[name]?.n ?? 0;
      check(
        "f5.performance_budgets",
        Object.keys(report.over).length === 0 && counted("apply") >= 80 && counted("render") >= 80 && counted("total") >= 80 &&
          counted("move") >= 30 && counted("scroll") >= 30,
        { scale, ...report },
      );
      await control.eval(`${win}.show(_k)\n${win}.selections.set(_sel12)\ned.buffers.kill(_perf)`);
      await settle();
    }

    // -- the socket is killed: reconnect and reattach to the same frame --
    const beforeDrop = await js<{ attaches: number; resets: number }>("window.neomacsDebug.snapshot()");
    (state as unknown as { socket: { destroy(): void } }).socket.destroy();
    let afterDrop = beforeDrop as { attaches: number; resets: number; frame?: number | null };
    for (let i = 0; i < 100 && afterDrop.attaches === beforeDrop.attaches; i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      afterDrop = await js("window.neomacsDebug.snapshot()");
    }
    await settle();
    type("back ");
    await settle();
    check(
      "f4.socket_killed_and_recovered",
      afterDrop.attaches === beforeDrop.attaches + 1 && state.frame === frame && (await pyText("len(ed.frames)")) === "1" &&
        (await pyText("_k.text")) === "'typed in the first, back shared text'" &&
        (await js<string>("document.querySelector('.nm-window .nm-content').textContent")) === "typed in the first, back shared text",
      { before: beforeDrop.attaches, after: afterDrop.attaches, frame: state.frame, text: await pyText("_k.text") },
    );

    // -- the engine dies: say so, start another, attach afresh --
    shell.answerEngineGone = () => true;
    const oldEngine = shell.engine();
    // The buffer "keys" has text that was never saved.  Give the journal its
    // moment to write it before the engine is killed (ENGINE 11.1).
    let journaled = "0";
    for (let i = 0; i < 80 && (journaled === "0" || (await pyText("ed.journal.pending")) === "True"); i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      journaled = await pyText("len(list(ed.journal.directory.glob('*.json')))");
    }
    oldEngine?.process.kill("SIGKILL");
    for (let i = 0; i < 200 && (shell.engineRestarts === 0 || state.frame === null || shell.engine() === oldEngine); i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const fresh = shell.control();
    await fresh?.call("engine.idle", 10);
    await frames(6);
    // The new engine found what the dead one left, and said so unasked.
    const offer = (await ui()).chrome.message;
    const left = (await fresh?.eval("[e.text for e in ed.journal.left_behind() if e.name == 'keys']")) as string;
    check(
      "step11.unsaved_text_is_offered_after_the_engine_dies",
      journaled !== "0" && offer.startsWith("Unsaved text from") && offer.endsWith("M-x recover") &&
        left === "['typed in the first, back shared text']",
      { journaled, offer, left },
    );
    type("alive");
    for (let i = 0; i < 60; i++) {
      await new Promise((resolve) => setTimeout(resolve, 50));
      if (((await fresh?.eval("ed.buffers.get('*scratch*').text")) as string) === "'alive'") break;
    }
    const revived = (await fresh?.eval("(len(ed.frames), ed.buffers.get('*scratch*').text, ed.buffers.get('keys') is None)")) as string;
    check(
      "f4.engine_gone_and_restarted",
      shell.engineRestarts === 1 && shell.engine() !== oldEngine && revived === "(1, 'alive', True)" &&
        shell.lastEngineGoneDetail.includes("The engine stopped (SIGKILL)") &&
        (await js<string>("document.querySelector('.nm-window .nm-content').textContent")) === "alive",
      { restarts: shell.engineRestarts, revived, detail: shell.lastEngineGoneDetail.slice(0, 80) },
    );
  } catch (error) {
    failures.push(`exception: ${String(error)}`);
  }
  clearTimeout(timer);
  console.log("SMOKE-RESULT " + JSON.stringify({ ok: failures.length === 0, failures, results }));
  return failures.length === 0 ? 0 : 1;
}
