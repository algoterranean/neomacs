// The renderer's entry point: connect, attach, apply redraw, render, acknowledge.

import { FaceSheet } from "./faces.ts";
import { FrameModel, type RedrawEvent, type ViewModel } from "./model.ts";
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
let frame = new FrameModel();
let viewElement: ViewElement | null = null;
let viewModel: ViewModel | null = null;
let renderQueued = false;
let pendingAcks: number[] = [];
let batches = 0;
let renders = 0;
let attachedFrame: number | null = null;

function status(text: string): void {
  viewElement?.destroy();
  viewElement = null;
  viewModel = null;
  const el = document.createElement("div");
  el.className = "nm-status";
  el.textContent = text;
  root.replaceChildren(el);
}

function mountMainView(): void {
  const main = frame.mainView;
  if (main === viewModel && viewElement) return;
  viewElement?.destroy();
  viewElement = null;
  viewModel = main;
  if (!main) return;
  const id = main.id;
  viewElement = new ViewElement(
    main,
    frame,
    (viewport) => rpc.notify("view.viewport", id, ...viewport),
    queueRender,
  );
  root.replaceChildren(viewElement.root);
  viewElement.mounted();
}

function render(): void {
  renderQueued = false;
  renders += 1;
  mountMainView();
  viewElement?.render();
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
    if (event[0] === "reset") faces.clear();
    else if (event[0] === "face_define") faces.define(event[1] as number, event[2] as Record<string, unknown>);
  }
  try {
    pendingAcks.push(...frame.apply(events as unknown as RedrawEvent[]));
  } catch (error) {
    // Something the frontend could not apply: ask for everything again.
    console.error(error);
    void rpc.call("ui.resync");
    return;
  }
  if (frame.resets !== before) viewModel = null; // the views were replaced
  queueRender();
});

async function attach(state: ConnectState): Promise<void> {
  rpc.abandon("reconnected");
  frame = new FrameModel();
  faces.clear();
  viewModel = null;
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

window.neomacsDebug = {
  snapshot: () => ({
    frame: attachedFrame,
    batches,
    renders,
    resets: frame.resets,
    faces: frame.faces.size,
    view: viewElement?.snapshot() ?? null,
  }),
  scrollTo: (y: number) => {
    if (viewElement) viewElement.scroller.scrollTop = y;
  },
  scrollBy: (dy: number) => {
    if (viewElement) viewElement.scroller.scrollTop += dy;
  },
  /** Resolves after the next two painted frames. */
  frames: (count = 2) =>
    new Promise<void>((resolve) => {
      const step = (left: number) => (left <= 0 ? resolve() : requestAnimationFrame(() => step(left - 1)));
      step(count);
    }),
};

status("Connecting to the engine…");
host.ready();
