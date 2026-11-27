// The renderer's entry point: connect, attach, apply redraw, render, acknowledge,
// send input and answer geometry requests.

import { Chrome } from "./chrome.ts";
import { FaceSheet } from "./faces.ts";
import { overBudget, Perf } from "./perf.ts";
import { applyTheme, themeVariables } from "./theme.ts";
import type { Position } from "./geometry.ts";
import { Input } from "./input.ts";
import { type Layout, Workspace } from "./layout.ts";
import { FrameModel, type RedrawEvent, type Run, type ViewModel } from "./model.ts";
import { Mouse } from "./mouse.ts";
import { type Json, RemoteError, Rpc } from "./protocol.ts";
import { ViewElement } from "./view.ts";

const PROTOCOL = 1;
const SEARCH_PLACE = "search";
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
  clipboardRead(which: string): Promise<string>;
  clipboardWrite(which: string, text: string): void;
  setMenu(items: unknown): void;
  onMenuCommand(callback: (command: string) => void): void;
  openFrame(options: unknown): void;
  pathsOf(files: File[]): string[];
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
const perf = new Perf();
const chrome = new Chrome(root);
// Views and regions are labelled, and messages announced (FRONTEND 1): enough
// to find one's way with a screen reader, not a full treatment.
root.setAttribute("role", "application");
root.setAttribute("aria-label", "Neomacs");
let batchSinceRender = false;
let frame = new FrameModel();
// The element for each view the frame has, by the model object it draws.
const elements = new Map<ViewModel, ViewElement>();
let mainElement: ViewElement | null = null;
// Views embedded in the text of another view (FRONTEND 5.6), and those that have been measured.
const inlineElements = new Set<ViewElement>();
const mounted = new WeakSet<ViewElement>();
let generation = 0; // goes up when the view elements are all replaced
let sentMenu: unknown = null;
const blobUrls = new Map<number, string>();
let renderQueued = false;
let pendingAcks: number[] = [];
let batches = 0;
let renders = 0;
let attachedFrame: number | null = null;
let attaches = 0;
let statusEl: HTMLDivElement | null = null;
let shownMessage: { text: string; kind: string } | null = null;

const mac = /Mac|iPhone|iPad/.test(navigator.platform);
const input = new Input(root, rpc, { mac, optionIsMeta: true }, () => focusedElement(), (ms) => perf.key(ms));

const workspace = new Workspace({
  viewRoot: (id) => {
    const model = frame.views.get(id);
    return model ? elementFor(model, false).root : null;
  },
  // The engine is the authority: say what was asked and wait for its `layout`.
  panelAction: (panel, action, arg) => {
    void rpc.call("ui.panel", panel, action, arg ?? null).catch((error: unknown) => console.error(error));
  },
  splitResized: (split, ratios) => {
    void rpc.call("view.split_resized", split, ratios).catch((error: unknown) => console.error(error));
  },
  positionRect: (view, line, offset) => {
    const model = frame.views.get(view);
    const element = model ? elements.get(model) : undefined;
    const caret = element?.geometry.caret(line, offset);
    return caret ? new DOMRect(caret.left, caret.top, caret.width, caret.bottom - caret.top) : null;
  },
});
chrome.main.prepend(workspace.root, workspace.floatingLayer);

ViewElement.onUserScroll = () => {
  perf.scrolled = true;
};
ViewElement.host = {
  resourceUrl: (id) => {
    const resource = frame.resources.get(id);
    if (!resource || resource.kind !== "image") return null;
    // A file is fetched through the main process, which serves only what the
    // engine defined for this frame (FRONTEND 9).
    if (typeof resource.source["path"] === "string") return attachedFrame === null ? null : `neomacs-resource://frame-${attachedFrame}/${id}`;
    const data = resource.source["data"];
    const mime = resource.source["mime"];
    if (typeof data !== "string" || typeof mime !== "string" || !/^image\/[a-z0-9.+-]+$/i.test(mime)) return null;
    let url = blobUrls.get(id);
    if (!url) {
      try {
        const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
        url = URL.createObjectURL(new Blob([bytes], { type: mime }));
        blobUrls.set(id, url);
      } catch {
        return null;
      }
    }
    return url;
  },
  inlineRoot: (id) => {
    const model = frame.views.get(id);
    if (!model) return null;
    const element = elementFor(model, true);
    inlineElements.add(element);
    return element.root;
  },
  resized: () => queueRender(),
};

function viewOf(target: Element): ViewElement | null {
  const root = target.closest(".nm-view");
  if (!root) return null;
  for (const element of elements.values()) if (element.root === root) return element;
  return null;
}

const mouse = new Mouse(root, rpc, {
  viewOf,
  pathsOf: (files) => host.pathsOf(files),
  middlePaste: () => {
    // The click has put the cursor there; now what was selected, or copied, goes in.
    void host.clipboardRead("primary").then((text) => {
      if (text.length > 0) rpc.notify("input.paste", text, "primary");
    });
  },
});

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
  // The main area and the panels.
  const before = new Map<ViewElement, Element | null>();
  for (const element of elements.values()) before.set(element, element.root.parentElement);
  for (const id of workspace.update(frame.layout as Layout | null, generation)) {
    const model = frame.views.get(id);
    if (model) live.add(model);
  }
  const main = frame.mainView;
  mainElement = (main ? elements.get(main) : undefined) ?? null;
  if (mainElement) status(null);
  // The prompt: the minibuffer the engine put in the prompt area.
  let prompt: ViewElement | null = null;
  let search: ViewElement | null = null;
  let label = "";
  for (const [id, shown] of frame.minibuffers) {
    const model = frame.views.get(id);
    if (!model) continue;
    if (shown.host["kind"] === "place" && shown.host["name"] === SEARCH_PLACE) {
      // The one named place this frontend offers: the search box.
      live.add(model);
      search = elementFor(model, true);
      chrome.setSearch(runsText(shown.options["label"]), search.root);
      continue;
    }
    if (shown.host["kind"] !== "prompt") continue;
    live.add(model);
    prompt = elementFor(model, true);
    prompt.minLines = Number(shown.options["min_lines"] ?? 1);
    prompt.maxLines = shown.options["multiline"] ? Number(shown.options["max_lines"] ?? 10) : 1;
    label = runsText(shown.options["label"]);
    chrome.setPrompt(label, prompt.root);
  }
  if (!prompt) chrome.setPrompt(null, null);
  if (!search) chrome.setSearch(null, null);
  // A minibuffer embedded in a line lives as long as that line is in the page.
  for (const element of inlineElements) {
    if (element.root.isConnected && frame.views.get(element.model.id) === element.model) live.add(element.model);
    else inlineElements.delete(element);
  }
  for (const [model, element] of elements) {
    if (!live.has(model)) {
      element.destroy();
      elements.delete(model);
      if (mainElement === element) mainElement = null;
      continue;
    }
    if (!element.root.isConnected) continue;
    if (!mounted.has(element)) {
      mounted.add(element);
      element.mounted();
    } else if (before.get(element) !== element.root.parentElement) {
      element.reparented();
    }
  }
  const popup = frame.popup;
  const promptModel = prompt ? prompt.model : null;
  chrome.setPopup(popup && promptModel && popup.view === promptModel.id ? popup.items : null, popup?.selected ?? 0);
}

function render(): void {
  const began = performance.now();
  const hadBatch = batchSinceRender;
  batchSinceRender = false;
  renderQueued = false;
  renders += 1;
  syncViews();
  const focused = focusedElement();
  for (const element of elements.values()) {
    element.focused = element === focused;
    if (element.root.isConnected) element.render();
  }
  // A line just drawn may have embedded a minibuffer: measure and draw it now.
  for (const element of inlineElements) {
    if (element.root.isConnected && !mounted.has(element)) {
      mounted.add(element);
      element.mounted();
      element.focused = element === focusedElement();
      element.render();
    }
  }
  workspace.placeFloating();
  if (frame.menu !== sentMenu) {
    // The application menu is the main process's to build (FRONTEND 8.4).
    sentMenu = frame.menu;
    host.setMenu(frame.menu);
  }
  if (frame.message && frame.message !== shownMessage) {
    shownMessage = frame.message;
    chrome.setMessage(frame.message.text, frame.message.kind);
  }
  chrome.setKeys(frame.keys);
  input.cursorMoved();
  if (document.title !== frame.title && frame.title) document.title = frame.title;
  // Have the style and layout work done now, so that it is counted here and
  // not in whatever script happens to ask a question next.
  void root.offsetHeight;
  perf.rendered(performance.now() - began, hadBatch);
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
  const began = performance.now();
  batches += 1;
  const before = frame.resets;
  let restyled = false;
  // Faces go to the stylesheet as they are defined.
  for (const event of events as unknown as RedrawEvent[]) {
    if (event[0] === "reset") {
      faces.clear();
      ViewElement.styleVersion += 1;
      restyled = true;
    } else if (event[0] === "face_define") {
      faces.define(event[1] as number, event[2] as Record<string, unknown>);
      ViewElement.styleVersion += 1; // sizes may have changed: views measure again
      restyled = true;
    } else if (event[0] === "ui_faces") {
      restyled = true;
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
    dropElements();
  }
  for (const element of elements.values()) element.batchApplied();
  // What is drawn around the text takes its colours from the theme too.
  if (restyled) applyTheme(document.documentElement, themeVariables(frame.faces, frame.uiFaces));
  batchSinceRender = true;
  perf.applied(performance.now() - began);
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

rpc.onRequest("layout.move", async (view, positions, unit, count) => {
  const began = performance.now();
  const places = positions as unknown as Position[];
  const answer = await withView(view, (v) => v.geometry.move(places, unit as string, count as number));
  perf.add("move", (performance.now() - began) / Math.max(1, places.length));
  return answer;
});
rpc.onRequest("layout.line_edge", (view, line, offset, which) =>
  withView(view, (v) => v.geometry.lineEdge(line as number, offset as number, which as string)),
);
rpc.onRequest("layout.rect", (view, line, offset) => withView(view, (v) => v.geometry.rect(line as number, offset as number)));
rpc.onRequest("layout.viewport", (view) => withView(view, (v) => Promise.resolve(v.viewportInfo())));

// -- what only the main process can do (FRONTEND 8.4, 10) --------------------------

rpc.onNotification("clipboard.set", (which, content) => {
  const text = (content as { text?: Json } | null)?.text;
  if (typeof text === "string") host.clipboardWrite(typeof which === "string" ? which : "clipboard", text);
});
rpc.onRequest("clipboard.get", async (which) => ({ text: await host.clipboardRead(typeof which === "string" ? which : "clipboard") }));
rpc.onNotification("ui.open_frame", (options) => host.openFrame(options ?? {}));
host.onMenuCommand((command) => {
  if (command === "yank") {
    // The menu's Paste: what is on the clipboard, sent as a paste.
    void host.clipboardRead("clipboard").then((text) => rpc.notify("input.paste", text, "clipboard"));
    return;
  }
  void rpc.call("engine.call", command, []).catch((error: unknown) => console.error(error));
});

// -- attaching -------------------------------------------------------------------

/** Throw away every view element: the views they drew have been replaced. */
function dropElements(): void {
  for (const element of elements.values()) element.destroy();
  elements.clear();
  inlineElements.clear();
  mainElement = null;
  generation += 1;
  for (const url of blobUrls.values()) URL.revokeObjectURL(url);
  blobUrls.clear();
}

async function attach(state: ConnectState): Promise<void> {
  rpc.abandon("reconnected");
  dropElements();
  attaches += 1;
  frame = new FrameModel();
  faces.clear();
  pendingAcks = [];
  if (state.live && state.frame !== null) {
    // The page was reloaded but the connection, and so the frame, survived.
    attachedFrame = state.frame;
    await rpc.call("ui.resync");
    return;
  }
  // "panels": this frontend draws window splits and panels (ENGINE 8.5).
  // "places": the parts of the window a minibuffer can be shown in besides the prompt (ENGINE 7.5).
  const options: { [key: string]: Json } = { protocol: PROTOCOL, capabilities: ["panels"], places: [SEARCH_PLACE] };
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
  /** Timings of the renderer's work since `perfReset`, and which are over budget. */
  perf: (scale = 1) => ({ summary: perf.summary(), over: overBudget(perf.summary(), scale as number) }),
  perfReset: () => perf.reset(),
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
    attaches,
    mouse: { sent: mouse.sent, drops: mouse.drops },
    layoutRebuilds: workspace.rebuilds,
  }),
  /** The arrangement as drawn: every view on show with its rectangle, and the panels. */
  arrangement: () => {
    const rect = (el: Element) => {
      const r = el.getBoundingClientRect();
      return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    };
    return {
      views: [...elements.values()]
        .filter((e) => e.root.isConnected)
        .map((e) => ({
          id: e.model.id,
          name: e.model.info.name ?? "",
          rect: rect(e.root),
          focused: e.root.classList.contains("nm-focused"),
          modeline: e.snapshot()["modeline"],
          inPanel: e.root.closest(".nm-panel")?.getAttribute("data-name") ?? null,
          inline: e.root.closest(".nm-inline-minibuffer") !== null,
          // The text of its lines, without the gutter.
          text: [...e.root.querySelectorAll(":scope > .nm-scroller > .nm-lines > .nm-line > .nm-content")].map((c) => c.textContent).join("\n"),
        })),
      panels: [...document.querySelectorAll<HTMLElement>(".nm-panel")].map((p) => ({
        id: Number(p.dataset["panel"]),
        name: p.dataset["name"],
        floating: p.classList.contains("nm-floating"),
        rect: rect(p),
        tabs: [...p.querySelectorAll(".nm-tab-title")].map((t) => t.textContent),
        active: p.querySelector(".nm-tab-active .nm-tab-title")?.textContent ?? null,
      })),
      dividers: [...document.querySelectorAll(".nm-divider")].map(rect),
      workspace: rect(workspace.root),
    };
  },
  /** The view showing the buffer of this name, as `snapshot().view` describes the main one. */
  viewNamed: (name: string) => {
    for (const element of elements.values()) if (element.model.info.name === name && element.root.isConnected) return element.snapshot();
    return null;
  },
  /** Where a position is on screen, in client coordinates: for aiming the mouse in tests. */
  pointAt: (view: number, line: number, offset: number) => {
    const model = frame.views.get(view);
    const caret = model ? elements.get(model)?.geometry.caret(line, offset) : null;
    return caret ? { x: caret.left + 1, y: (caret.top + caret.bottom) / 2, width: caret.width } : null;
  },
  images: () =>
    [...document.querySelectorAll<HTMLImageElement>("img.nm-image, img.nm-gutter-image")].map((img) => ({
      src: img.src,
      complete: img.complete,
      naturalWidth: img.naturalWidth,
      width: Math.round(img.getBoundingClientRect().width),
      alt: img.alt,
    })),
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
