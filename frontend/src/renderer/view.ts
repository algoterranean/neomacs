// One view in the DOM (FRONTEND 5.1, 5.4): only the lines near the viewport
// exist as elements; the rest is a spacer of estimated height.

import { HeightIndex } from "./heights.ts";
import type { FrameModel, Line, Run, ViewModel } from "./model.ts";

export type Viewport = [first: number, last: number, pageLines: number];

function runSpan(run: Run): HTMLSpanElement {
  const span = document.createElement("span");
  if (run[2] !== 0) span.className = `f${run[2]}`;
  if (run[0] !== null) span.dataset["o"] = String(run[0]);
  span.textContent = run[1]; // text, never markup
  return span;
}

export class ViewElement {
  readonly root: HTMLDivElement;
  readonly scroller: HTMLDivElement;
  private spacer: HTMLDivElement;
  private linesEl: HTMLDivElement;
  private modelineEl: HTMLDivElement;
  private model: ViewModel;
  private frame: FrameModel;
  private heights: HeightIndex;
  private elements = new Map<Line, HTMLDivElement>();
  private domFirst = 1;
  private domLast = 0;
  private reported: string | null = null;
  private settingScroll = false;
  private lastWidth = 0;
  private onViewport: (viewport: Viewport) => void;
  private requestRender: () => void;

  constructor(
    model: ViewModel,
    frame: FrameModel,
    onViewport: (viewport: Viewport) => void,
    requestRender: () => void,
  ) {
    this.model = model;
    this.frame = frame;
    this.onViewport = onViewport;
    this.requestRender = requestRender;
    this.root = document.createElement("div");
    this.root.className = "nm-view";
    this.scroller = document.createElement("div");
    this.scroller.className = "nm-scroller";
    this.spacer = document.createElement("div");
    this.spacer.className = "nm-spacer";
    this.linesEl = document.createElement("div");
    this.linesEl.className = "nm-lines";
    this.modelineEl = document.createElement("div");
    this.modelineEl.className = "nm-modeline";
    this.scroller.append(this.spacer, this.linesEl);
    this.root.append(this.scroller, this.modelineEl);
    this.heights = new HeightIndex(model, 20);
    model.observer = this.heights;
    this.scroller.addEventListener("scroll", () => this.scrolled(), { passive: true });
  }

  /** Call once the element is in the document: measures the default line height. */
  mounted(): void {
    const probe = document.createElement("div");
    probe.className = "nm-line";
    const content = document.createElement("div");
    content.className = "nm-content";
    content.textContent = "x";
    probe.append(content);
    this.linesEl.append(probe);
    const height = probe.getBoundingClientRect().height;
    probe.remove();
    if (height > 0) this.heights.estimate = height;
  }

  destroy(): void {
    if (this.model.observer === this.heights) this.model.observer = null;
    this.root.remove();
  }

  // -- scrolling ------------------------------------------------------------

  private scrolled(): void {
    if (this.settingScroll) return;
    // The user scrolled: the viewport now starts wherever that put it.
    const at = this.lineAtScroll(this.scroller.scrollTop);
    this.model.topLine = at.line;
    this.model.topOffset = at.offset;
    this.requestRender();
  }

  /**
   * The line at the top of the viewport for a scroll offset the browser reports.
   * Scroll offsets are rounded to device pixels, so one meant to sit exactly on
   * a line's top edge can come back a fraction short of it.
   */
  private lineAtScroll(scrollTop: number): { line: number; offset: number } {
    const at = this.heights.lineAt(scrollTop + 0.5);
    return { line: at.line, offset: Math.max(0, scrollTop - this.heights.top(at.line)) };
  }

  private reveal(line: number, align: string): void {
    const h = this.heights;
    const page = this.scroller.clientHeight;
    const top = h.top(line);
    const bottom = top + h.height(line);
    const current = h.top(this.model.topLine) + this.model.topOffset;
    let target = current;
    if (align === "top") target = top;
    else if (align === "bottom") target = bottom - page;
    else if (align === "center") target = top - (page - h.height(line)) / 2;
    else if (top < current) target = top;
    else if (bottom > current + page) target = bottom - page;
    else return;
    const at = h.lineAt(Math.max(0, target));
    this.model.topLine = at.line;
    this.model.topOffset = at.offset;
  }

  // -- rendering ------------------------------------------------------------

  private gutterColumns(): { id: string; kind: string; width: string }[] {
    const digits = Math.max(2, String(this.model.lineCount).length);
    return this.model.info.gutter
      .filter((c) => (c.side ?? "left") === "left")
      .map((c) => ({
        id: c.id,
        kind: c.kind,
        width: c.kind === "numbers" ? `${digits + 1}ch` : `${(c.width ?? 1) + 1}ch`,
      }));
  }

  private buildLine(line: Line): HTMLDivElement {
    const el = document.createElement("div");
    el.className = "nm-line";
    const gutterFace = this.frame.uiFaces["gutter"] ?? 0;
    for (const column of this.gutterColumns()) {
      const cell = document.createElement("span");
      cell.className = `nm-gutter-col f${gutterFace}`;
      cell.dataset["column"] = column.id;
      cell.dataset["kind"] = column.kind;
      cell.style.width = column.width;
      if (column.kind !== "numbers") {
        const mark = line[0].gutter?.[column.id];
        if (mark) {
          cell.textContent = mark[0];
          if (mark[1] !== 0) cell.className = `nm-gutter-col f${mark[1]}`;
        }
      }
      el.append(cell);
    }
    const content = document.createElement("div");
    content.className = "nm-content";
    for (const run of line[1]) content.append(runSpan(run));
    el.append(content);
    return el;
  }

  /** Bring the DOM into line with the model.  Called once per frame at most. */
  render(): void {
    const model = this.model;
    const h = this.heights;
    const info = model.info;
    this.root.classList.toggle("nm-nowrap", !info.wrap);
    this.linesEl.style.tabSize = String(Math.max(1, info.tab_width));

    // A change of width re-wraps everything: measured heights are stale.
    const width = this.scroller.clientWidth;
    if (width !== this.lastWidth) {
      this.lastWidth = width;
      h.invalidate();
    }

    if (model.pendingReveal) {
      const [line, , align] = model.pendingReveal;
      model.pendingReveal = null;
      this.reveal(Math.max(1, Math.min(line, model.lineCount)), align);
    }
    model.topLine = Math.max(1, Math.min(model.topLine, model.lineCount));

    const page = Math.max(1, this.scroller.clientHeight);
    // Lay out twice at most: measuring the lines can move them.
    for (let pass = 0; pass < 2; pass++) {
      const scrollTop = h.top(model.topLine) + model.topOffset;
      const firstVisible = model.topLine;
      const lastVisible = h.lineAt(scrollTop + page - 1).line;
      const margin = Math.max(10, lastVisible - firstVisible + 1);
      const first = Math.max(model.first, firstVisible - margin);
      const last = Math.min(model.last, lastVisible + margin);
      this.syncLines(first, last);
      if (!this.measure()) break;
    }
    h.refineEstimate();

    const total = h.total;
    this.spacer.style.height = `${total}px`;
    this.linesEl.style.transform = `translateY(${h.top(this.domFirst)}px)`;
    const maxScroll = Math.max(0, total - page);
    const scrollTop = Math.min(maxScroll, h.top(model.topLine) + model.topOffset);
    if (Math.abs(this.scroller.scrollTop - scrollTop) > 0.5) {
      this.settingScroll = true;
      this.scroller.scrollTop = scrollTop;
      this.settingScroll = false;
    }

    this.renderModeline();
    this.report(scrollTop, page);
  }

  private syncLines(first: number, last: number): void {
    const model = this.model;
    const wanted = new Set<Line>();
    const ordered: HTMLDivElement[] = [];
    for (let n = first; n <= last; n++) {
      const line = model.line(n);
      if (!line) continue;
      wanted.add(line);
      let el = this.elements.get(line);
      if (!el) {
        el = this.buildLine(line);
        this.elements.set(line, el);
      }
      el.dataset["line"] = String(n);
      const number = el.querySelector<HTMLElement>('[data-kind="numbers"]');
      if (number && number.textContent !== String(n)) number.textContent = String(n);
      ordered.push(el);
    }
    for (const [line, el] of this.elements) {
      if (!wanted.has(line)) {
        el.remove();
        this.elements.delete(line);
      }
    }
    // Put the elements in order, moving only those that are out of place.
    let cursor = this.linesEl.firstElementChild;
    for (const el of ordered) {
      if (el === cursor) cursor = cursor.nextElementSibling;
      else this.linesEl.insertBefore(el, cursor);
    }
    this.domFirst = ordered.length > 0 ? first : model.topLine;
    this.domLast = ordered.length > 0 ? last : this.domFirst - 1;
    if (ordered.length > 0 && !model.line(first)) this.domFirst = Number(ordered[0]?.dataset["line"]);
  }

  /** Record the heights of the lines in the DOM.  Returns true if any changed. */
  private measure(): boolean {
    let changed = false;
    for (const el of this.elements.values()) {
      const line = Number(el.dataset["line"]);
      if (this.heights.measure(line, el.getBoundingClientRect().height)) changed = true;
    }
    return changed;
  }

  private renderModeline(): void {
    const runs = this.model.modeline ?? [];
    const key = JSON.stringify(runs);
    if (this.modelineEl.dataset["key"] === key) return;
    this.modelineEl.dataset["key"] = key;
    this.modelineEl.replaceChildren(...runs.map(runSpan));
    const face = runs[0]?.[2] ?? 0;
    this.modelineEl.className = `nm-modeline f${face}`;
  }

  private report(scrollTop: number, page: number): void {
    const first = this.heights.lineAt(scrollTop).line;
    const last = this.heights.lineAt(scrollTop + page - 1).line;
    const pageLines = Math.max(1, Math.round(page / this.heights.estimate));
    const key = `${first},${last},${pageLines}`;
    if (key !== this.reported) {
      this.reported = key;
      this.onViewport([first, last, pageLines]);
    }
  }

  // -- for tests and diagnostics ----------------------------------------------

  snapshot(): Record<string, unknown> {
    const scrollTop = this.scroller.scrollTop;
    const page = this.scroller.clientHeight;
    const first = this.lineAtScroll(scrollTop).line;
    const last = this.heights.lineAt(scrollTop + page - 1).line;
    let missing = 0;
    for (let n = first; n <= last; n++) if (!this.model.line(n)) missing += 1;
    const firstEl = this.linesEl.querySelector<HTMLElement>(`[data-line="${first}"]`);
    const scrollerTop = this.scroller.getBoundingClientRect().top;
    return {
      lineCount: this.model.lineCount,
      held: this.model.lines.length > 0 ? [this.model.first, this.model.last] : null,
      dom: [this.domFirst, this.domLast],
      domElements: this.linesEl.childElementCount,
      visible: [first, last],
      missingVisible: missing,
      scrollTop,
      scrollHeight: this.scroller.scrollHeight,
      clientHeight: page,
      estimate: this.heights.estimate,
      firstVisibleText: firstEl?.querySelector(".nm-content")?.textContent ?? null,
      firstVisibleY: firstEl ? firstEl.getBoundingClientRect().top - scrollerTop : null,
      modeline: this.modelineEl.textContent,
    };
  }
}
