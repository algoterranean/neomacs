// What the frontend holds: the result of applying `redraw` events (ENGINE 4.3).
// This mirrors neomacs/frontends/model.py, which is the reference.

export type Run = [offset: number | null, text: string, face: number];
export type LineAttrs = { gutter?: Record<string, [string, number]>; [key: string]: unknown };
export type Line = [attrs: LineAttrs, runs: Run[]];
export type Selection = [anchorLine: number, anchorOffset: number, headLine: number, headOffset: number];
export type FaceAttrs = Record<string, unknown>;
export type RedrawEvent = [name: string, ...args: unknown[]];

export interface ViewInfo {
  name?: string;
  line_count: number;
  wrap: boolean;
  tab_width: number;
  gutter: { id: string; kind: string; side?: string; mode?: string; width?: number }[];
  margins?: { left: number; right: number };
  max_width?: number | null;
}

export class ProtocolError extends Error {}

/** Told about every change to the held lines, so layout state can follow it. */
export interface LineObserver {
  /** Lines [index, index + removed) of the held array were replaced by `added` lines. */
  spliced(index: number, removed: number, added: number): void;
  /** The held array was replaced outright. */
  replaced(): void;
}

export class ViewModel {
  readonly id: number;
  info: ViewInfo = { line_count: 1, wrap: true, tab_width: 8, gutter: [] };
  first = 1; // line number of lines[0]
  lines: Line[] = [];
  selections: Selection[] = [];
  primary = 0;
  shape = "block";
  modeline: Run[] | null = null;
  // Scroll position, kept on the same text across splices: the first visible
  // line, and how far into it (in pixels) the viewport starts.
  topLine = 1;
  topOffset = 0;
  pendingReveal: [line: number, offset: number, align: string] | null = null;
  observer: LineObserver | null = null;

  constructor(id: number) {
    this.id = id;
  }

  get lineCount(): number {
    return this.info.line_count;
  }

  get last(): number {
    return this.first + this.lines.length - 1;
  }

  line(number: number): Line | undefined {
    return this.lines[number - this.first];
  }

  hold(first: number, last: number): void {
    if (this.lines.length === 0 || first > last) {
      this.lines = [];
    } else {
      const lo = Math.max(first, this.first);
      const hi = Math.min(last, this.last);
      this.lines = lo <= hi ? this.lines.slice(lo - this.first, hi - this.first + 1) : [];
      this.first = lo;
    }
    this.observer?.replaced();
  }

  splice(first: number, removed: number, payload: Line[] | number): void {
    let added: number;
    if (typeof payload === "number") {
      added = payload;
      // Never touches held lines: either wholly above them, or below.
      if (this.lines.length > 0 && first + removed <= this.first) this.first += added - removed;
    } else {
      added = payload.length;
      if (this.lines.length === 0) {
        this.first = first;
        this.lines = payload.slice();
        this.observer?.replaced();
      } else if (first < this.first) {
        if (first + removed < this.first) {
          throw new ProtocolError(`lines at ${first} would leave a gap before ${this.first}`);
        }
        const drop = first + removed - this.first;
        this.lines.splice(0, drop, ...payload);
        this.first = first;
        this.observer?.spliced(0, drop, added);
      } else {
        const index = first - this.first;
        if (index > this.lines.length) {
          throw new ProtocolError(`lines at ${first} would leave a gap after ${this.last}`);
        }
        const gone = Math.min(removed, this.lines.length - index);
        this.lines.splice(index, removed, ...payload);
        this.observer?.spliced(index, gone, added);
      }
    }
    if (first + removed <= this.topLine) {
      // Lines changed above the viewport: stay on the same text.  The one
      // exception is a viewport at the very top, which stays at the top.
      if (!(this.topLine === 1 && this.topOffset === 0)) this.topLine += added - removed;
    } else if (first <= this.topLine) {
      // The top line itself was replaced.  Stay on whatever is there now; the
      // offset into it is clamped to its new height when it is laid out.
      this.topLine = Math.max(first, Math.min(this.topLine, first + added - 1));
    }
  }
}

export class FrameModel {
  faces = new Map<number, FaceAttrs>();
  uiFaces: Record<string, number> = {};
  layout: { main: [string, number]; panels: unknown[] } | null = null;
  views = new Map<number, ViewModel>();
  focus: number | null = null;
  title = "";
  resets = 0;

  view(id: number): ViewModel {
    let view = this.views.get(id);
    if (!view) {
      view = new ViewModel(id);
      this.views.set(id, view);
    }
    return view;
  }

  get mainView(): ViewModel | null {
    if (!this.layout || this.layout.main[0] !== "win") return null;
    return this.view(this.layout.main[1]);
  }

  /** Apply one batch.  Returns the sequence numbers of the flushes in it. */
  apply(events: RedrawEvent[]): number[] {
    const flushed: number[] = [];
    for (const [name, ...a] of events) {
      switch (name) {
        case "flush":
          flushed.push(a[0] as number);
          break;
        case "reset":
          this.faces.clear();
          this.uiFaces = {};
          this.layout = null;
          this.views.clear();
          this.focus = null;
          this.resets += 1;
          break;
        case "face_define":
          this.faces.set(a[0] as number, a[1] as FaceAttrs);
          break;
        case "ui_faces":
          this.uiFaces = a[0] as Record<string, number>;
          break;
        case "layout":
          this.layout = a[0] as FrameModel["layout"];
          break;
        case "title":
          this.title = a[0] as string;
          break;
        case "focus":
          this.focus = a[0] as number;
          break;
        case "win_info":
          this.view(a[0] as number).info = a[1] as ViewInfo;
          break;
        case "held":
          this.view(a[0] as number).hold(a[1] as number, a[2] as number);
          break;
        case "lines":
          this.view(a[0] as number).splice(a[1] as number, a[2] as number, a[3] as Line[] | number);
          break;
        case "selections": {
          const view = this.view(a[0] as number);
          view.primary = a[1] as number;
          view.selections = a[2] as Selection[];
          view.shape = a[3] as string;
          break;
        }
        case "modeline":
          this.view(a[0] as number).modeline = a[1] as Run[];
          break;
        case "reveal":
          this.view(a[0] as number).pendingReveal = [a[1] as number, a[2] as number, a[3] as string];
          break;
        default:
          throw new ProtocolError(`unknown redraw event: ${name}`);
      }
    }
    return flushed;
  }
}
