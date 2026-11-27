// The main area and the panels (FRONTEND 5.6): the window tree as nested flex
// containers with draggable dividers, docked panels with a tab strip, and
// floating panels over the top.  Nothing about the arrangement changes here
// on the user's say-so: an action is sent to the engine and the page waits for
// the `layout` event that answers it.

import type { PanelInfo } from "./model.ts";

export type Anchor = string | { view: number; line: number; offset: number };

export interface LayoutHost {
  /** The element of a view, created if need be, to be placed in the layout. */
  viewRoot(id: number): HTMLElement | null;
  panelAction(panel: number, action: string, arg?: string | number): void;
  splitResized(split: number, ratios: number[]): void;
  /** Where a position in a view is on screen, for a panel that floats beside it. */
  positionRect(view: number, line: number, offset: number): DOMRect | null;
}

export interface Layout {
  main: unknown[];
  panels: PanelInfo[];
}

const SIDES = ["left", "right", "top", "bottom"] as const;

function div(className: string): HTMLDivElement {
  const el = document.createElement("div");
  el.className = className;
  return el;
}

/** Follow a pointer from a press until it is released. */
function drag(event: PointerEvent, move: (e: PointerEvent) => void, done: (e: PointerEvent) => void): void {
  const target = event.currentTarget as HTMLElement;
  event.preventDefault();
  event.stopPropagation();
  target.setPointerCapture(event.pointerId);
  const onMove = (e: PointerEvent) => move(e);
  const onUp = (e: PointerEvent) => {
    target.removeEventListener("pointermove", onMove);
    target.removeEventListener("pointerup", onUp);
    target.removeEventListener("pointercancel", onUp);
    if (target.hasPointerCapture(e.pointerId)) target.releasePointerCapture(e.pointerId);
    done(e);
  };
  target.addEventListener("pointermove", onMove);
  target.addEventListener("pointerup", onUp);
  target.addEventListener("pointercancel", onUp);
}

export class Workspace {
  readonly root: HTMLDivElement;
  private floating: HTMLDivElement;
  private host: LayoutHost;
  private key = "";
  private placed: number[] = [];
  private floats: { el: HTMLDivElement; panel: PanelInfo }[] = [];
  /** Counts rebuilds of the DOM, so tests can tell that nothing was rebuilt. */
  rebuilds = 0;

  constructor(host: LayoutHost) {
    this.host = host;
    this.root = div("nm-workspace");
    this.floating = div("nm-floating-layer");
  }

  /** The layer floating panels are drawn in; it goes over the workspace. */
  get floatingLayer(): HTMLDivElement {
    return this.floating;
  }

  /**
   * Make the DOM match `layout`.  `generation` changes when the view elements
   * were replaced, which forces a rebuild.  Returns the views now on show.
   */
  update(layout: Layout | null, generation: number): number[] {
    const key = `${generation}:${JSON.stringify(layout)}`;
    if (key === this.key) return this.placed;
    this.key = key;
    this.rebuilds += 1;
    this.placed = [];
    this.floats = [];
    if (!layout) {
      this.root.replaceChildren();
      this.floating.replaceChildren();
      return this.placed;
    }
    // Docked panels take space from the area in order, the first outermost.
    const shown = layout.panels.filter((p) => p.visible && p.active !== null && p.tabs.length > 0);
    let inner: HTMLElement = this.tree(layout.main);
    for (const panel of shown.filter((p) => p.mode === "docked").reverse()) {
      const side = (SIDES as readonly string[]).includes(panel.side ?? "") ? (panel.side as string) : "bottom";
      const wrap = div(side === "left" || side === "right" ? "nm-dock nm-dock-row" : "nm-dock nm-dock-column");
      const el = this.panel(panel, side);
      inner.classList.add("nm-dock-rest");
      if (side === "left" || side === "top") wrap.append(el, inner);
      else wrap.append(inner, el);
      inner = wrap;
    }
    this.root.replaceChildren(inner);
    const floats: HTMLElement[] = [];
    for (const panel of shown.filter((p) => p.mode !== "docked")) {
      const el = this.panel(panel, null);
      this.floats.push({ el, panel });
      floats.push(el);
    }
    this.floating.replaceChildren(...floats);
    return this.placed;
  }

  private place(id: number, into: HTMLElement): void {
    const root = this.host.viewRoot(id);
    if (!root) return;
    into.append(root);
    this.placed.push(id);
  }

  private tree(node: unknown[]): HTMLElement {
    if (node[0] === "win") {
      const el = div("nm-window");
      el.dataset["view"] = String(node[1]);
      this.place(node[1] as number, el);
      return el;
    }
    const [, id, direction, ratios, children] = node as [string, number, string, number[], unknown[][]];
    const el = div(direction === "h" ? "nm-split nm-split-h" : "nm-split nm-split-v");
    el.dataset["split"] = String(id);
    const cells: HTMLElement[] = [];
    const shares = [...ratios];
    children.forEach((child, index) => {
      const cell = this.tree(child);
      cell.style.flex = `${shares[index] ?? 1} 1 0`;
      cells.push(cell);
      if (index > 0) el.append(this.divider(id, direction, cells, shares, index - 1));
      el.append(cell);
    });
    return el;
  }

  /** The bar between cells `index` and `index + 1`: drag it to share their space differently. */
  private divider(split: number, direction: string, cells: HTMLElement[], shares: number[], index: number): HTMLElement {
    const bar = div("nm-divider");
    bar.addEventListener("pointerdown", (event) => {
      const a = cells[index] as HTMLElement;
      const b = cells[index + 1] as HTMLElement;
      const horizontal = direction === "h";
      const sizeA = horizontal ? a.getBoundingClientRect().width : a.getBoundingClientRect().height;
      const sizeB = horizontal ? b.getBoundingClientRect().width : b.getBoundingClientRect().height;
      const both = (shares[index] ?? 1) + (shares[index + 1] ?? 1);
      const start = horizontal ? event.clientX : event.clientY;
      const apply = (e: PointerEvent) => {
        const moved = (horizontal ? e.clientX : e.clientY) - start;
        const size = Math.max(40, Math.min(sizeA + sizeB - 40, sizeA + moved));
        shares[index] = (both * size) / (sizeA + sizeB);
        shares[index + 1] = both - (shares[index] as number);
        // Shown at once; the engine's `layout` then says the same.
        a.style.flex = `${shares[index]} 1 0`;
        b.style.flex = `${shares[index + 1]} 1 0`;
      };
      drag(event, apply, (e) => {
        apply(e);
        this.host.splitResized(split, shares.map((s) => Math.round(s * 10000) / 10000));
      });
    });
    return bar;
  }

  private panel(panel: PanelInfo, side: string | null): HTMLDivElement {
    const el = div(side ? `nm-panel nm-panel-${side}` : "nm-panel nm-floating");
    el.dataset["panel"] = String(panel.id);
    el.dataset["name"] = panel.name;
    const size = Math.max(0.08, Math.min(0.9, panel.size));
    if (side) el.style.flex = `0 0 ${size * 100}%`;
    else el.style.height = `${Math.max(20, size * 100)}%`;

    const strip = div("nm-tabs");
    for (const tab of panel.tabs) {
      const button = div(tab.view === panel.active ? "nm-tab nm-tab-active" : "nm-tab");
      button.dataset["view"] = String(tab.view);
      const title = document.createElement("span");
      title.className = "nm-tab-title";
      title.textContent = tab.title; // text, never markup
      const close = document.createElement("span");
      close.className = "nm-tab-close";
      close.textContent = "×";
      close.title = "Close";
      close.addEventListener("pointerdown", (e) => e.stopPropagation());
      close.addEventListener("click", (e) => {
        e.stopPropagation();
        this.host.panelAction(panel.id, "close_tab", tab.view);
      });
      button.append(title, close);
      button.addEventListener("pointerdown", (e) => e.stopPropagation());
      button.addEventListener("click", () => this.host.panelAction(panel.id, "activate_tab", tab.view));
      strip.append(button);
    }
    const tools = div("nm-tab-tools");
    const tool = (label: string, title: string, action: string, arg?: string) => {
      const b = document.createElement("span");
      b.className = "nm-tab-tool";
      b.dataset["action"] = action;
      b.textContent = label;
      b.title = title;
      b.addEventListener("pointerdown", (e) => e.stopPropagation());
      b.addEventListener("click", () => this.host.panelAction(panel.id, action, arg));
      tools.append(b);
    };
    if (side) tool("⧉", "Float", "float");
    else tool("▁", "Dock at the bottom", "dock", "bottom");
    tool("–", "Hide", "hide");
    strip.append(tools);
    // Drag the strip to a side of the frame to dock the panel there, or to the middle to float it.
    strip.addEventListener("pointerdown", (event) => {
      let moved = false;
      const from = { x: event.clientX, y: event.clientY };
      drag(
        event,
        (e) => {
          moved ||= Math.hypot(e.clientX - from.x, e.clientY - from.y) > 12;
          el.classList.toggle("nm-dragging", moved);
        },
        (e) => {
          el.classList.remove("nm-dragging");
          if (!moved) return;
          const target = this.dropTarget(e.clientX, e.clientY);
          if (target === "float") {
            if (side) this.host.panelAction(panel.id, "float");
          } else if (target !== side) {
            this.host.panelAction(panel.id, "dock", target);
          }
        },
      );
    });

    const body = div("nm-panel-body");
    if (panel.active !== null) this.place(panel.active, body);
    el.append(strip, body);
    if (side) {
      // The edge facing the main area resizes the panel.
      const grip = div("nm-panel-grip");
      grip.addEventListener("pointerdown", (event) => {
        const box = this.root.getBoundingClientRect();
        const horizontal = side === "left" || side === "right";
        const total = horizontal ? box.width : box.height;
        let fraction = size;
        const apply = (e: PointerEvent) => {
          const at = horizontal ? e.clientX - box.left : e.clientY - box.top;
          const taken = side === "left" || side === "top" ? at : total - at;
          fraction = Math.max(0.08, Math.min(0.9, taken / Math.max(1, total)));
          el.style.flex = `0 0 ${fraction * 100}%`;
        };
        drag(event, apply, (e) => {
          apply(e);
          this.host.panelAction(panel.id, "resize", Math.round(fraction * 1000) / 1000);
        });
      });
      el.append(grip);
    }
    return el;
  }

  /** Which side of the frame a point is nearest, or "float" for the middle. */
  dropTarget(x: number, y: number): string {
    const box = this.root.getBoundingClientRect();
    const fx = (x - box.left) / Math.max(1, box.width);
    const fy = (y - box.top) / Math.max(1, box.height);
    const edge = 0.25;
    const candidates: [number, string][] = [
      [fx, "left"],
      [1 - fx, "right"],
      [fy, "top"],
      [1 - fy, "bottom"],
    ];
    candidates.sort((a, b) => a[0] - b[0]);
    const nearest = candidates[0] as [number, string];
    return nearest[0] < edge ? nearest[1] : "float";
  }

  /** Put the floating panels at their anchors.  Called after the views have been drawn. */
  placeFloating(): void {
    if (this.floats.length === 0) return;
    const box = this.floating.getBoundingClientRect();
    for (const { el, panel } of this.floats) {
      const anchor = (panel as PanelInfo & { anchor?: Anchor }).anchor ?? "center";
      const width = Math.min(box.width - 16, Math.max(320, box.width * 0.6));
      el.style.width = `${width}px`;
      const height = el.getBoundingClientRect().height;
      let left = (box.width - width) / 2;
      let top = (box.height - height) / 2;
      if (typeof anchor === "string") {
        if (anchor.includes("top")) top = 8;
        if (anchor.includes("bottom")) top = box.height - height - 8;
        if (anchor.includes("left")) left = 8;
        if (anchor.includes("right")) left = box.width - width - 8;
      } else {
        const at = this.host.positionRect(anchor.view, anchor.line, anchor.offset);
        if (at) {
          // Under the position if there is room, otherwise above it.
          const below = at.bottom - box.top + 4;
          top = below + height <= box.height ? below : Math.max(0, at.top - box.top - height - 4);
          left = Math.max(8, Math.min(box.width - width - 8, at.left - box.left));
        }
      }
      el.style.left = `${Math.max(0, left)}px`;
      el.style.top = `${Math.max(0, top)}px`;
    }
  }
}
