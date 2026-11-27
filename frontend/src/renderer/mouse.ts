// Mouse, and files dropped on a view (FRONTEND 8.3).  Every event is hit-tested
// here and sent as a position in the text; the engine decides what it means.

import type { Rpc } from "./protocol.ts";
import type { ViewElement } from "./view.ts";

export interface MouseHost {
  /** The view an element belongs to, if it is inside one. */
  viewOf(target: Element): ViewElement | null;
  pathsOf(files: File[]): string[];
  /** A middle click: paste the selection (or clipboard) where the click was. */
  middlePaste(): void;
}

function mods(event: MouseEvent): string {
  return (event.ctrlKey ? "C" : "") + (event.altKey ? "M" : "") + (event.shiftKey ? "S" : "") + (event.metaKey ? "s" : "");
}

export class Mouse {
  private rpc: Rpc;
  private host: MouseHost;
  private dragging: { view: ViewElement; button: number; mods: string } | null = null;
  private pendingDrag: MouseEvent | null = null;
  private lastDrag = "";
  sent = 0;
  drops = 0;

  constructor(root: HTMLElement, rpc: Rpc, host: MouseHost) {
    this.rpc = rpc;
    this.host = host;
    root.addEventListener("mousedown", (event) => this.down(event));
    window.addEventListener("mousemove", (event) => this.move(event));
    window.addEventListener("mouseup", (event) => this.up(event));
    root.addEventListener("contextmenu", (event) => event.preventDefault());
    root.addEventListener("dragover", (event) => {
      // Without this the browser would navigate to a dropped file.
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
    root.addEventListener("drop", (event) => this.drop(event));
  }

  private send(view: ViewElement, event: MouseEvent, action: string, button: number, clicks: number, modifiers: string): boolean {
    const hit = view.hit(event.target instanceof Element ? event.target : null, event.clientX, event.clientY);
    if (!hit) return false;
    if (action === "drag") {
      // A drag that has not reached another position says nothing new.
      const key = `${view.model.id}:${hit.line}:${hit.offset}`;
      if (key === this.lastDrag) return true;
      this.lastDrag = key;
    }
    this.sent += 1;
    this.rpc.notify("input.mouse", button, action, clicks, modifiers, view.model.id, hit.area, hit.line, hit.offset);
    return true;
  }

  private down(event: MouseEvent): void {
    const view = event.target instanceof Element ? this.host.viewOf(event.target) : null;
    if (!view) return; // a tab, a divider, the palette: not text
    event.preventDefault();
    const button = event.button + 1; // 1 left, 2 middle, 3 right
    const modifiers = mods(event);
    // The operating system's double-click interval is known here, so the count is too.
    const pressed = view.hit(event.target instanceof Element ? event.target : null, event.clientX, event.clientY);
    if (!this.send(view, event, "press", button, Math.max(1, Math.min(3, event.detail)), modifiers)) return;
    // A drag that ends where it began is a click: nothing more to say.
    this.lastDrag = pressed ? `${view.model.id}:${pressed.line}:${pressed.offset}` : "";
    this.dragging = { view, button, mods: modifiers };
    if (button === 2) this.host.middlePaste();
  }

  private move(event: MouseEvent): void {
    if (!this.dragging || event.buttons === 0) return;
    // At most one drag per frame, for the latest position.
    const first = this.pendingDrag === null;
    this.pendingDrag = event;
    if (!first) return;
    requestAnimationFrame(() => {
      const latest = this.pendingDrag;
      this.pendingDrag = null;
      const dragging = this.dragging;
      if (latest && dragging) this.send(dragging.view, latest, "drag", dragging.button, 1, dragging.mods);
    });
  }

  private up(event: MouseEvent): void {
    const dragging = this.dragging;
    if (!dragging) return;
    // Where the button came up is where the drag ended.  A movement still
    // waiting for its frame would otherwise be lost, and a quick drag with it:
    // the release itself selects nothing.  (A drag to a position already
    // reported is not sent again.)
    if (this.pendingDrag !== null || event.buttons === 0) this.send(dragging.view, event, "drag", dragging.button, 1, dragging.mods);
    this.dragging = null;
    this.pendingDrag = null;
    this.send(dragging.view, event, "release", dragging.button, 1, dragging.mods);
  }

  private drop(event: DragEvent): void {
    event.preventDefault();
    const files = [...(event.dataTransfer?.files ?? [])];
    const paths = this.host.pathsOf(files);
    if (paths.length === 0) return;
    const view = event.target instanceof Element ? this.host.viewOf(event.target) : null;
    const hit = view?.hit(event.target instanceof Element ? event.target : null, event.clientX, event.clientY);
    if (!view || !hit) return;
    this.drops += 1;
    this.rpc.notify("input.drop", paths, view.model.id, hit.line, hit.offset);
  }
}
