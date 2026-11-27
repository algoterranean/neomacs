// The renderer's entry point: connect, attach, apply redraw, render, acknowledge,
// send input and answer geometry requests.

import { Chrome } from "./chrome.ts";
import { FaceSheet } from "./faces.ts";
import type { Position } from "./geometry.ts";
import { Input } from "./input.ts";
import { FrameModel, type RedrawEvent, type Run, type ViewModel } from "./model.ts";
import { type Json, RemoteError, Rpc } from "./protocol.ts";
import { ViewElement } from "./view.ts";

const PROTOCOL = 1;
const NO_SUCH_FRAME = -32002;

interface ConnectState {
  live: boolean; // the connection the previous page used is still open
  frame: number | null;
  token: string | null;
}

interface Host {
  send(line: string): void;
  onLine(callback: (line: string) => void): void;
  onConnect(callback: (state: ConnectState) => void): void;
  onStatus(callback: (text: string) => void): void;
  attached(frame: number, token: string): void;
  ready(): void;
}

declare global {
  interface Window {
    neomacsHost: Host;
    neomacsDebug: Record<string, unknown>;
  }
}

const host = window.neomacsHost;
const root = document.getElementById("root") as HTMLDivElement;
const rpc = new Rpc((line) => host.send(line));
const faces = new FaceSheet(document);
const chrome = new Chrome(root);
let frame = new FrameModel();
// The element for each view the frame has, by the model object it draws.
const elements = new Map<ViewModel, ViewElement>();
let mainElement: ViewElement | null = null;
let renderQueued = false;
let pendingAcks: number[] = [];
let batches = 0;
let renders = 0;
let attachedFrame: number | null = null;
let statusEl: HTMLDivElement | null = null;
let shownMessage: { text: string; kind: string } | null = null;

const mac = /Mac|iPhone|iPad/.test(navigator.platform);
const input = new Input(root, rpc, { mac, optionIsMeta: true }, () => focusedElement());

function focusedElement(): ViewElement | null {
  const model = frame.focus !== null ? frame.views.get(frame.focus) : undefined;
  return (model ? elements.get(model) : undefined) ?? mainElement;
}

function status(text: string | null): void {
  statusEl?.remove();
  statusEl = null;
  if (text === null) return;
  statusEl = document.createElement("div");
  statusEl.className = "nm-status";
  statusEl.textContent = text;
  chrome.main.prepend(statusEl);
}

function elementFor(model: ViewModel, mini: boolean): ViewElement {
  let element = elements.get(model);
  if (!element) {
    const id = model.id;
    element = new ViewElement(model, frame, (viewport) => rpc.notify("view.viewport", id, ...viewport), queueRender, mini);
    elements.set(model, element);
  }
  return element;
}

function runsText(runs: unknown): string {
  return Array.isArray(runs) ? (runs as Run[]).map((run) => (typeof run[1] === "string" ? run[1] : "")).join("") : "";
}

/** Create, place and remove view elements to match the model. */
function syncViews(): void {
  const live = new Set<ViewModel>();
  const main = frame.mainView;
  if (main) {
    live.add(main);
    const element = elementFor(main, false);
    if (mainElement !== element) {
      mainElement?.root.remove();
      mainElement = element;
      chrome.main.prepend(element.root);
      element.mounted();
      status(null);
    }
  }
  // The prompt: the minibuffer the engine put in the prompt area.
  let prompt: ViewElement | null = null;
  let label = "";
  for (const [id, shown] of frame.minibuffers) {
    if (shown.host["kind"] !== "prompt") continue; // named places are stage F3
    const model = frame.views.get(id);
    if (!model) continue;
    live.add(model);
    const fresh = !elements.has(model);
    prompt = elementFor(model, true);
    prompt.minLines = Number(shown.options["min_lines"] ?? 1);
    prompt.maxLines = shown.options["multiline"] ? Number(shown.options["max_lines"] ?? 10) : 1;
    label = runsText(shown.options["label"]);
    chrome.setPrompt(label, prompt.root);
    if (fresh) prompt.mounted();
  }
  if (!prompt) chrome.setPrompt(null, null);
  for (const [model, element] of elements) {
    if (!live.has(model)) {
      element.destroy();
      elements.delete(model);
      if (mainElement === element) mainElement = null;
    }
  }
  const popup = frame.popup;
  const promptModel = prompt ? prompt.model : null;
  chrome.setPopup(popup && promptModel && popup.view === promptModel.id ? popup.items : null, popup?.selected ?? 0);
}

function render(): void {
  renderQueued = false;
  renders += 1;
  syncViews();
  const focused = focusedElement();
  for (const element of elements.values()) {
    element.focused = element === focused;
    element.render();
  }
  if (frame.message && frame.message !== shownMessage) {
    shownMessage = frame.message;
    chrome.setMessage(frame.message.text, frame.message.kind);
  }
  chrome.setKeys(frame.keys);
  input.cursorMoved();
  if (document.title !== frame.title && frame.title) document.title = frame.title;
  const acks = pendingAcks;
  pendingAcks = [];
  if (acks.length === 0) return;
  const send = () => {
    for (const seq of acks) rpc.notify("view.ack", seq);
  };
  // Acknowledge once the frame has been painted; at once if nothing is painting.
  if (document.visibilityState === "hidden") send();
  else requestAnimationFrame(send);
}

function queueRender(): void {
  if (renderQueued) return;
  renderQueued = true;
  if (document.visibilityState === "hidden") queueMicrotask(render);
  else requestAnimationFrame(render);
}

rpc.onNotification("redraw", (events) => {
  batches += 1;
  const before = frame.resets;
  // Faces go to the stylesheet as they are defined.
  for (const event of events as unknown as RedrawEvent[]) {
    if (event[0] === "reset") {
      faces.clear();
      ViewElement.styleVersion += 1;
    } else if (event[0] === "face_define") {
      faces.define(event[1] as number, event[2] as Record<string, unknown>);
      ViewElement.styleVersion += 1; // sizes may have changed: views measure again
    }
  }
  try {
    pendingAcks.push(...frame.apply(events as unknown as RedrawEvent[]));
  } catch (error) {
    // Something the frontend could not apply: ask for everything again.
    console.error(error);
    void rpc.call("ui.resync");
    return;
  }
  if (frame.resets !== before) {
    // The views were replaced: their elements go too.
    for (const element of elements.values()) element.destroy();
    elements.clear();
    mainElement = null;
  }
  for (const element of elements.values()) element.batchApplied();
  queueRender();
});

// -- geometry requests (ENGINE 4.4, FRONTEND 7) ------------------------------

/** Run a geometry request against a view whose DOM is up to date with the model. */
async function withView<T>(id: Json, body: (view: ViewElement) => Promise<T>): Promise<T> {
  // The engine sent any pending update first, and it has been applied to the
  // model; put it in the DOM before measuring anything.
  if (renderQueued) render();
  const model = frame.views.get(id as number);
  const element = model ? elements.get(model) : undefined;
  if (!element) throw new Error(`no such view: ${String(id)}`);
  try {
    return await body(element);
  } finally {
    element.endGeometry();
    queueRender();
  }
}

rpc.onRequest("layout.move", (view, positions, unit, count) =>
  withView(view, (v) => v.geometry.move(positions as unknown as Position[], unit as string, count as number)),
);
rpc.onRequest("layout.line_edge", (view, line, offset, which) =>
  withView(view, (v) => v.geometry.lineEdge(line as number, offset as number, which as string)),
);
rpc.onRequest("layout.rect", (view, line, offset) => withView(view, (v) => v.geometry.rect(line as number, offset as number)));
rpc.onRequest("layout.viewport", (view) => withView(view, (v) => Promise.resolve(v.viewportInfo())));

// -- attaching -------------------------------------------------------------------

async function attach(state: ConnectState): Promise<void> {
  rpc.abandon("reconnected");
  for (const element of elements.values()) element.destroy();
  elements.clear();
  mainElement = null;
  frame = new FrameModel();
  faces.clear();
  pendingAcks = [];
  if (state.live && state.frame !== null) {
    // The page was reloaded but the connection, and so the frame, survived.
    attachedFrame = state.frame;
    await rpc.call("ui.resync");
    return;
  }
  const options: { [key: string]: Json } = { protocol: PROTOCOL, capabilities: [] };
  let reply: Json;
  try {
    if (state.frame !== null && state.token !== null) {
      reply = await rpc.call("ui.attach", { ...options, frame: state.frame, token: state.token });
    } else {
      reply = await rpc.call("ui.attach", options);
    }
  } catch (error) {
    if (error instanceof RemoteError && error.code === NO_SUCH_FRAME) {
      reply = await rpc.call("ui.attach", options); // it expired: start afresh
    } else {
      status(`Could not attach to the engine: ${String(error)}`);
      return;
    }
  }
  const info = reply as { frame: number; token: string };
  attachedFrame = info.frame;
  host.attached(info.frame, info.token);
}

host.onLine((line) => rpc.handleLine(line));
host.onStatus((text) => status(text));
host.onConnect((state) => void attach(state));
new ResizeObserver(() => queueRender()).observe(root);
document.addEventListener("visibilitychange", () => queueRender());

// -- for tests and diagnostics -----------------------------------------------------

const frames = (count = 2) =>
  new Promise<void>((resolve) => {
    const step = (left: number) => (left <= 0 ? resolve() : requestAnimationFrame(() => step(left - 1)));
    step(count);
  });

window.neomacsDebug = {
  snapshot: () => ({
    frame: attachedFrame,
    batches,
    renders,
    resets: frame.resets,
    faces: frame.faces.size,
    focus: frame.focus,
    mainView: mainElement?.model.id ?? null,
    view: mainElement?.snapshot() ?? null,
    selections: mainElement?.model.selections ?? [],
    chrome: chrome.snapshot(),
    input: { keys: input.keysSent, texts: input.textsSent, editContext: input.hasEditContext },
    activeIsRoot: document.activeElement === root,
  }),
  metrics: () => mainElement?.metrics() ?? null,
  coverage: () => mainElement?.coverage() ?? null,
  lineY: (line: number) => mainElement?.lineY(line) ?? null,
  /** The primary cursor's position relative to the left edge of its line's text. */
  cursorX: () => {
    const view = mainElement;
    const selection = view?.model.selections[view.model.primary];
    if (!view || !selection) return null;
    const caret = view.geometry.caret(selection[2], selection[3]);
    const content = view.lineElement(selection[2])?.querySelector(".nm-content")?.getBoundingClientRect();
    return caret && content ? { x: caret.left - content.left, line: selection[2], offset: selection[3], height: caret.bottom - caret.top } : null;
  },
  setCursorAnimation: (on: boolean) => {
    ViewElement.cursorAnimation = on;
  },
  scrollTo: (y: number) => {
    if (mainElement) mainElement.scroller.scrollTop = y;
  },
  scrollBy: (dy: number) => {
    if (mainElement) mainElement.scroller.scrollTop += dy;
  },
  /**
   * Scroll by `step` pixels every frame for `steps` frames, entirely inside the
   * page so nothing but the frame clock paces it.  Each record describes what
   * the previous frame left on screen.
   */
  scrollRun: (step: number, steps: number) =>
    new Promise((resolve) => {
      const records: { missingModel: number; missingDom: number; blank: number }[] = [];
      const started = performance.now();
      const tick = () => {
        const view = mainElement;
        if (!view) return resolve({ error: "no view" });
        if (records.length > 0 || steps === 0) records.push(view.coverage());
        else records.push({ missingModel: 0, missingDom: 0, blank: 0 });
        if (records.length > steps) {
          const bad = records.map((r) => r.missingDom > 0 || r.blank > 1);
          let longest = 0;
          let run = 0;
          for (const b of bad) {
            run = b ? run + 1 : 0;
            longest = Math.max(longest, run);
          }
          const ms = performance.now() - started;
          return resolve({
            step,
            steps,
            ms: Math.round(ms),
            pxPerSecond: Math.round((step * steps * 1000) / ms),
            badFrames: bad.filter(Boolean).length,
            longestRun: longest,
            worstBlank: Math.max(...records.map((r) => r.blank)),
            worstMissing: Math.max(...records.map((r) => r.missingDom)),
          });
        }
        view.scroller.scrollTop += step;
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }),
  /**
   * Scroll by `step` pixels every frame and check that the text moves by exactly
   * that much: corrections to estimated heights must not show as jumps.
   */
  steadyRun: (step: number, steps: number) =>
    new Promise((resolve) => {
      let anchor: number | null = null;
      let expected: number | null = null;
      let worst = 0;
      let measured = 0;
      const tick = (left: number) => {
        const view = mainElement;
        if (!view) return resolve({ error: "no view" });
        if (anchor !== null && expected !== null) {
          const y = view.lineY(anchor);
          if (y !== null) {
            worst = Math.max(worst, Math.abs(y - expected));
            measured += 1;
          }
        }
        if (left <= 0) return resolve({ step, steps, measured, worstJump: Math.round(worst * 100) / 100 });
        const snap = view.snapshot() as { visible: [number, number] };
        anchor = Math.round((snap.visible[0] + snap.visible[1]) / 2);
        const before = view.lineY(anchor);
        const top = view.scroller.scrollTop;
        view.scroller.scrollTop += step;
        const moved = view.scroller.scrollTop - top; // less than `step` at either end
        expected = before === null ? null : before - moved;
        requestAnimationFrame(() => requestAnimationFrame(() => tick(left - 1)));
      };
      requestAnimationFrame(() => tick(steps));
    }),
  frames,
};

status("Connecting to the engine…");
host.ready();
