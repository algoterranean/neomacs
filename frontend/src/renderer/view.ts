// One view in the DOM (FRONTEND 5.1, 5.4): only the lines near the viewport
// exist as elements; the rest is a spacer of estimated height.

import { Geometry } from "./geometry.ts";
import { HeightIndex } from "./heights.ts";
import type { FrameModel, Line, Run, ViewModel } from "./model.ts";
import { cpLength } from "./positions.ts";

export type Viewport = [first: number, last: number, pageLines: number];

function runSpan(run: Run): HTMLSpanElement {
  const span = document.createElement("span");
  if (typeof run[1] !== "string") {
    // An inline object.  Drawing images and embedded minibuffers is stage F3;
    // until then it is a placeholder.
    span.className = "nm-object";
    if (run[0] !== null) {
      span.dataset["o"] = String(run[0]);
      span.dataset["n"] = String((run[1] as { covers?: number }).covers ?? 0);
    }
    span.textContent = "\u25af";
    return span;
  }
  if (run[2] !== 0) span.className = `f${run[2]}`;
  if (run[0] !== null) {
    // Where this run's text is in the buffer line, in code points (FRONTEND 6).
    span.dataset["o"] = String(run[0]);
    span.dataset["n"] = String(cpLength(run[1]));
  }
  span.textContent = run[1]; // text, never markup
  return span;
}

export class ViewElement {
  readonly root: HTMLDivElement;
  readonly scroller: HTMLDivElement;
  readonly model: ViewModel;
  readonly geometry: Geometry;
  /** A minibuffer: no modeline, and as tall as its text within limits. */
  readonly mini: boolean;
  /** Whether this view has the keyboard: its cursor is solid and blinks. */
  focused = false;
  /** Set while a key is auto-repeating: the cursor then moves without animating. */
  repeating = false;
  minLines = 1;
  maxLines = 1;
  private spacer: HTMLDivElement;
  private linesEl: HTMLDivElement;
  private selectionLayer: HTMLDivElement;
  private cursorLayer: HTMLDivElement;
  private preeditEl: HTMLSpanElement;
  private cursorEls: HTMLDivElement[] = [];
  private modelineEl: HTMLDivElement;
  private frame: FrameModel;
  private heights: HeightIndex;
  private charWidth = 8;
  private lineHeight = 20; // of a row in the default face; `heights.estimate` is an average
  private seenStyle = -1;
  private extra: [number, number] | null = null; // lines a geometry request needs laid out
  private batchWaiters: (() => void)[] = [];
  private drawnDomFirst = -1;
  private lastCursor: { x: number; y: number } | null = null;
  private elements = new Map<Line, HTMLDivElement>();
  private domFirst = 1;
  private domLast = 0;
  private reported: string | null = null;
  private settingScroll = false;
  private knownScrollTop = 0; // the scroll offset the model's position corresponds to
  private lastWidth = 0;
  private onViewport: (viewport: Viewport) => void;
  private requestRender: () => void;

  constructor(
    model: ViewModel,
    frame: FrameModel,
    onViewport: (viewport: Viewport) => void,
    requestRender: () => void,
    mini = false,
  ) {
    this.mini = mini;
    this.model = model;
    this.frame = frame;
    this.onViewport = onViewport;
    this.requestRender = requestRender;
    this.root = document.createElement("div");
    this.root.className = mini ? "nm-view nm-mini" : "nm-view";
    this.scroller = document.createElement("div");
    this.scroller.className = "nm-scroller";
    this.spacer = document.createElement("div");
    this.spacer.className = "nm-spacer";
    this.linesEl = document.createElement("div");
    this.linesEl.className = "nm-lines";
    this.modelineEl = document.createElement("div");
    this.modelineEl.className = "nm-modeline";
    this.selectionLayer = document.createElement("div");
    this.selectionLayer.className = "nm-layer nm-selections";
    this.cursorLayer = document.createElement("div");
    this.cursorLayer.className = "nm-layer nm-cursors";
    this.preeditEl = document.createElement("span");
    this.preeditEl.className = "nm-preedit";
    this.preeditEl.hidden = true;
    this.cursorLayer.append(this.preeditEl);
    // Selections are drawn under the text and cursors over it.
    this.scroller.append(this.spacer, this.selectionLayer, this.linesEl, this.cursorLayer);
    this.root.append(this.scroller);
    if (!mini) this.root.append(this.modelineEl);
    this.geometry = new Geometry(this);
    this.heights = new HeightIndex(model, 20);
    model.observer = this.heights;
    this.scroller.addEventListener("scroll", () => this.scrolled(), { passive: true });
  }

  /** Bumped whenever a face is defined or the faces are cleared. */
  static styleVersion = 0;

  /** Call once the element is in the document. */
  mounted(): void {
    this.measureDefaults();
  }

  /** Measure a row and a character of the default face. */
  private measureDefaults(): void {
    this.seenStyle = ViewElement.styleVersion;
    const probe = document.createElement("div");
    probe.className = "nm-line";
    const content = document.createElement("div");
    content.className = "nm-content";
    content.textContent = "0000000000";
    probe.append(content);
    this.linesEl.append(probe);
    const height = probe.getBoundingClientRect().height;
    const range = document.createRange();
    range.selectNodeContents(content);
    const width = range.getBoundingClientRect().width / 10;
    probe.remove();
    if (height > 0) {
      this.lineHeight = height;
      this.heights.estimate = height;
    }
    if (width > 0) this.charWidth = width;
  }

  destroy(): void {
    if (this.model.observer === this.heights) this.model.observer = null;
    this.root.remove();
  }

  // -- scrolling ------------------------------------------------------------

  private scrolled(): void {
    if (this.settingScroll) return;
    if (this.adoptScroll()) this.requestRender();
  }

  /**
   * If the scroller is not where this view last put it, the user scrolled: the
   * viewport now starts wherever that left it.  Called for scroll events, and at
   * the start of every render, because a render can run before the event for a
   * scroll that has already happened and must not put the old position back.
   */
  private adoptScroll(): boolean {
    const scrollTop = this.scroller.scrollTop;
    if (Math.abs(scrollTop - this.knownScrollTop) <= 0.5) return false;
    this.knownScrollTop = scrollTop;
    const at = this.lineAtScroll(scrollTop);
    this.model.topLine = at.line;
    this.model.topOffset = at.offset;
    return true;
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

    // A change of width re-wraps everything, and a change of faces can change
    // every size: either way the measured heights are stale.
    const width = this.scroller.clientWidth;
    if (width !== this.lastWidth || this.seenStyle !== ViewElement.styleVersion) {
      this.lastWidth = width;
      h.invalidate();
      if (this.seenStyle !== ViewElement.styleVersion) this.measureDefaults();
    }

    this.adoptScroll();
    if (model.pendingReveal) {
      const [line, , align] = model.pendingReveal;
      model.pendingReveal = null;
      this.reveal(Math.max(1, Math.min(line, model.lineCount)), align);
    }
    model.topLine = Math.max(1, Math.min(model.topLine, model.lineCount));

    if (this.mini) {
      // A minibuffer is as tall as its text, between its minimum and maximum.
      const lines = Math.max(this.minLines, Math.min(this.maxLines, model.lineCount));
      this.scroller.style.height = `${lines * this.lineHeight}px`;
    }
    const page = Math.max(1, this.scroller.clientHeight);
    // Lay out twice at most: measuring the lines can move them.
    for (let pass = 0; pass < 2; pass++) {
      model.topOffset = Math.max(0, Math.min(model.topOffset, h.height(model.topLine) - 1));
      const scrollTop = h.top(model.topLine) + model.topOffset;
      const firstVisible = model.topLine;
      const lastVisible = h.lineAt(scrollTop + page - 1).line;
      const margin = Math.max(10, lastVisible - firstVisible + 1);
      let first = Math.max(model.first, firstVisible - margin);
      let last = Math.min(model.last, lastVisible + margin);
      if (this.extra && this.extra[1] - this.extra[0] < 1000) {
        first = Math.max(model.first, Math.min(first, this.extra[0]));
        last = Math.min(model.last, Math.max(last, this.extra[1]));
      }
      this.syncLines(first, last);
      if (!this.measure()) break;
    }
    h.refineEstimate();

    const total = h.total;
    this.spacer.style.height = `${total}px`;
    const offset = `translateY(${h.top(this.domFirst)}px)`;
    this.linesEl.style.transform = offset;
    this.selectionLayer.style.transform = offset;
    this.cursorLayer.style.transform = offset;
    const maxScroll = Math.max(0, total - page);
    const scrollTop = Math.min(maxScroll, h.top(model.topLine) + model.topOffset);
    if (Math.abs(this.scroller.scrollTop - scrollTop) > 0.5) {
      this.settingScroll = true;
      this.scroller.scrollTop = scrollTop;
      this.settingScroll = false;
    }
    this.knownScrollTop = this.scroller.scrollTop;

    this.drawSelections();
    if (!this.mini) this.renderModeline();
    this.report(scrollTop, page);
  }

  // -- selections and cursors (FRONTEND 5.5) ----------------------------------

  private drawSelections(): void {
    const model = this.model;
    const geometry = this.geometry;
    const origin = this.linesEl.getBoundingClientRect();
    const selectionFace = this.frame.uiFaces["selection"] ?? 0;
    const cursorFace = this.frame.uiFaces["cursor"] ?? 0;
    const highlights: HTMLDivElement[] = [];
    const box = (left: number, top: number, width: number, height: number, cls: string) => {
      const el = document.createElement("div");
      el.className = cls;
      el.style.left = `${left - origin.left}px`;
      el.style.top = `${top - origin.top}px`;
      el.style.width = `${width}px`;
      el.style.height = `${height}px`;
      return el;
    };

    let shown = 0;
    const dense = model.selections.length > 8;
    model.selections.forEach((selection, index) => {
      const [anchorLine, anchorOffset, headLine, headOffset] = selection;
      const forward = anchorLine < headLine || (anchorLine === headLine && anchorOffset <= headOffset);
      const [startLine, startOffset, endLine, endOffset] = forward
        ? [anchorLine, anchorOffset, headLine, headOffset]
        : [headLine, headOffset, anchorLine, anchorOffset];
      // The selected text, a rectangle per piece of each row.
      for (let n = Math.max(startLine, this.domFirst); n <= Math.min(endLine, this.domLast); n++) {
        const from = n === startLine ? startOffset : 0;
        const to = n === endLine ? endOffset : geometry.lineLength(n);
        const a = geometry.caret(n, from);
        const b = geometry.caret(n, to);
        if (!a || !b) continue;
        if (to > from) {
          const bands = geometry.bands(n);
          const first = geometry.bandOf(bands, a);
          const last = geometry.bandOf(bands, b);
          const content = this.lineElement(n)?.querySelector(".nm-content")?.getBoundingClientRect();
          for (let i = first; i <= last; i++) {
            const band = bands[i];
            if (!band || !content) continue;
            const left = i === first ? a.left : content.left;
            const right = i === last ? b.left : content.right;
            highlights.push(box(left, band.top, Math.max(0, right - left), band.bottom - band.top, `nm-selection f${selectionFace}`));
          }
        }
        if (n < endLine) {
          // The line break is selected too: show it as a sliver at the end of the line.
          highlights.push(box(b.left, b.top, this.charWidth * 0.6, b.bottom - b.top, `nm-selection f${selectionFace}`));
        }
      }
      // The cursor, at the head.
      const caret = headLine >= this.domFirst && headLine <= this.domLast ? geometry.caret(headLine, headOffset) : null;
      if (!caret) return;
      let el = this.cursorEls[shown];
      if (!el) {
        el = document.createElement("div");
        this.cursorEls[shown] = el;
        this.cursorLayer.append(el);
      }
      shown += 1;
      const shape = this.focused ? model.shape : "hollow";
      const primary = index === model.primary;
      el.className = `nm-cursor nm-cursor-${shape} f${cursorFace}${primary ? " nm-primary" : ""}${this.focused ? " nm-blink" : ""}`;
      const x = caret.left - origin.left;
      const y = caret.top - origin.top;
      const width = shape === "bar" ? 2 : caret.width || this.charWidth;
      el.style.width = `${width}px`;
      el.style.height = `${caret.bottom - caret.top}px`;
      if (primary) {
        // Glide, unless that would read as lag (FRONTEND F12).
        const far = this.lastCursor !== null && Math.abs(y - this.lastCursor.y) > this.scroller.clientHeight;
        const relaid = this.drawnDomFirst !== this.domFirst;
        const still = !ViewElement.cursorAnimation || dense || far || relaid || this.repeating || this.lastCursor === null;
        this.cursorLayer.classList.toggle("nm-no-anim", still);
        this.lastCursor = { x, y };
      }
      el.style.transform = `translate(${x}px, ${y}px)`;
    });
    for (const el of this.cursorEls.splice(shown)) el.remove();
    this.selectionLayer.replaceChildren(...highlights);
    this.drawnDomFirst = this.domFirst;
    if (!this.preeditEl.hidden) this.placePreedit();
  }

  /** The primary cursor's rectangle in client coordinates, for the input method. */
  cursorRect(): DOMRect | null {
    const el = this.cursorLayer.querySelector(".nm-primary");
    return el ? el.getBoundingClientRect() : null;
  }

  /** Show text an input method is composing, at the primary cursor.  Empty hides it. */
  setPreedit(text: string): void {
    this.preeditEl.hidden = text.length === 0;
    this.preeditEl.textContent = text;
    this.placePreedit();
  }

  private placePreedit(): void {
    const selection = this.model.selections[this.model.primary];
    const caret = selection ? this.geometry.caret(selection[2], selection[3]) : null;
    if (!caret) return;
    const origin = this.linesEl.getBoundingClientRect();
    this.preeditEl.style.transform = `translate(${caret.left - origin.left}px, ${caret.top - origin.top}px)`;
  }

  // -- for geometry requests ----------------------------------------------------

  static cursorAnimation = true;

  lineElement(line: number): HTMLDivElement | null {
    const held = this.model.line(line);
    const el = held ? this.elements.get(held) : undefined;
    return el && el.isConnected ? el : null;
  }

  /** Whole rows that fit in the view, less one: what a page motion moves by. */
  pageRows(): number {
    return Math.max(1, Math.floor(this.scroller.clientHeight / this.lineHeight) - 1);
  }

  /** Scroll by a number of rows of the default height. */
  scrollRows(rows: number): void {
    this.scroller.scrollTop += rows * this.lineHeight;
    this.adoptScroll();
  }

  /** Called when a batch from the engine has been applied to the model. */
  batchApplied(): void {
    for (const waiter of this.batchWaiters.splice(0)) waiter();
  }

  /**
   * Make sure lines `first` to `last` are held and laid out, asking the engine
   * for them if need be.  Throws if they have not arrived within 250 ms; the
   * engine then falls back to motion by logical line (FRONTEND 7).
   */
  async ensureLines(first: number, last: number): Promise<void> {
    const deadline = performance.now() + 250;
    first = Math.max(1, first);
    last = Math.min(this.model.lineCount, last);
    for (;;) {
      let held = true;
      for (let n = first; n <= last; n++) if (!this.model.line(n)) held = false;
      if (held) {
        let laidOut = true;
        for (let n = first; n <= last; n++) if (!this.lineElement(n)) laidOut = false;
        if (!laidOut) {
          this.extra = this.extra ? [Math.min(this.extra[0], first), Math.max(this.extra[1], last)] : [first, last];
          this.render();
        }
        return;
      }
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error(`lines ${first} to ${last} are not available`);
      const page = Math.max(1, Math.round(this.scroller.clientHeight / this.heights.estimate));
      this.reported = null; // the next render reports the real viewport again
      this.onViewport([first, last, page]);
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, remaining);
        this.batchWaiters.push(() => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
  }

  /** A geometry request has been answered: its extra lines need not stay laid out. */
  endGeometry(): void {
    this.extra = null;
  }

  viewportInfo(): { first: number; last: number; page_lines: number } {
    const scrollTop = this.scroller.scrollTop;
    const page = Math.max(1, this.scroller.clientHeight);
    return {
      first: this.lineAtScroll(scrollTop).line,
      last: this.heights.lineAt(scrollTop + page - 1).line,
      page_lines: Math.max(1, Math.floor(page / this.lineHeight)),
    };
  }

  metrics(): { charWidth: number; lineHeight: number; rows: number; contentWidth: number } {
    const content = this.linesEl.querySelector<HTMLElement>(".nm-content");
    return {
      charWidth: this.charWidth,
      lineHeight: this.lineHeight,
      rows: Math.floor(this.scroller.clientHeight / this.lineHeight),
      contentWidth: content ? content.getBoundingClientRect().width : this.scroller.clientWidth,
    };
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

  /**
   * What is wrong with the viewport as it stands in the DOM right now: visible
   * lines the model does not hold, visible lines with no element, and pixels of
   * the viewport that no line element covers.
   */
  coverage(): { missingModel: number; missingDom: number; blank: number } {
    const scrollTop = this.scroller.scrollTop;
    const page = this.scroller.clientHeight;
    const first = this.lineAtScroll(scrollTop).line;
    const last = this.heights.lineAt(scrollTop + page - 1).line;
    let missingModel = 0;
    let missingDom = 0;
    for (let n = first; n <= last; n++) {
      const line = this.model.line(n);
      if (!line) missingModel += 1;
      if (!line || !this.elements.get(line)?.isConnected) missingDom += 1;
    }
    const box = this.scroller.getBoundingClientRect();
    const firstEl = this.linesEl.firstElementChild;
    const lastEl = this.linesEl.lastElementChild;
    let blank = page;
    if (firstEl && lastEl) {
      const top = firstEl.getBoundingClientRect().top;
      const bottom = lastEl.getBoundingClientRect().bottom;
      const atEnd = Number((lastEl as HTMLElement).dataset["line"]) >= this.model.lineCount;
      blank = Math.max(0, Math.min(page, top - box.top)) + (atEnd ? 0 : Math.max(0, Math.min(page, box.bottom - bottom)));
    }
    return { missingModel, missingDom, blank: Math.round(blank) };
  }

  /** Screen position of a line's element relative to the top of the viewport, if it is in the DOM. */
  lineY(number: number): number | null {
    const el = this.linesEl.querySelector<HTMLElement>(`[data-line="${number}"]`);
    return el ? el.getBoundingClientRect().top - this.scroller.getBoundingClientRect().top : null;
  }

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
      cursors: this.cursorEls.length,
      selections: this.selectionLayer.childElementCount,
      cursorAnimated: !this.cursorLayer.classList.contains("nm-no-anim"),
    };
  }
}
