// What the frontend holds: the result of applying `redraw` events (ENGINE 4.3).
// This mirrors neomacs/frontends/model.py, which is the reference.

// A text run, or an inline object standing for buffer text (an image, a minibuffer).
export type Run = [offset: number | null, text: string, face: number];
export type ObjectRun = [offset: number | null, object: Record<string, unknown>];
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
  /** Runs for the right-hand end of the modeline. */
  modelineRight: Run[] = [];
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

export interface PanelInfo {
  id: number;
  name: string;
  visible: boolean;
  mode: string;
  side?: string;
  size: number;
  tabs: { view: number; title: string }[];
  active: number | null;
}

/** A region of the workbench (WORKSPACE 5.1): shown or not, and how wide in ems if it has a width. */
export interface RegionState {
  shown: boolean;
  width?: number;
}

/** A project, as the project sidebar lists it. */
export interface WorkspaceInfo {
  id: number;
  name: string;
  root: string | null;
  current: boolean;
  unsaved: number;
}

/** An item of the status bar.  One with a `command` runs it when clicked. */
export interface StatusItem {
  text: string;
  command?: string;
  title?: string;
}

export class FrameModel {
  // The workbench, as the engine last described it (WORKSPACE 6).
  regions: Record<string, RegionState> = {};
  workspaces: WorkspaceInfo[] = [];
  statusLeft: StatusItem[] = [];
  statusRight: StatusItem[] = [];
  faces = new Map<number, FaceAttrs>();
  // Images and other resources by id.  Drawing them is stage F3.
  resources = new Map<number, { kind: string; source: Record<string, unknown> }>();
  uiFaces: Record<string, number> = {};
  // `main` is ["win", id] or ["split", id, direction, ratios, children].
  layout: { main: unknown[]; panels: PanelInfo[] } | null = null;
  lastWindow: number | null = null;
  views = new Map<number, ViewModel>();
  focus: number | null = null;
  title = "";
  resets = 0;
  // Chrome the engine describes (FRONTEND 5.6).  Drawing it is stage F2 and F3.
  minibuffers = new Map<number, { host: Record<string, unknown>; options: Record<string, unknown> }>();
  popup: { items: [string, string][]; selected: number; view: number; line: number; offset: number } | null = null;
  message: { text: string; kind: string } | null = null;
  keys = "";
  menu: unknown[] = [];

  view(id: number): ViewModel {
    let view = this.views.get(id);
    if (!view) {
      view = new ViewModel(id);
      this.views.set(id, view);
    }
    return view;
  }

  /** The ids of the windows in the main area, in reading order. */
  windows(): number[] {
    const found: number[] = [];
    const walk = (node: unknown[]): void => {
      if (node[0] === "win") found.push(node[1] as number);
      else for (const child of node[4] as unknown[][]) walk(child);
    };
    if (this.layout) walk(this.layout.main);
    return found;
  }

  /** Every view the layout mentions: windows and the tabs of panels. */
  layoutViews(): Set<number> {
    const found = new Set(this.windows());
    for (const panel of this.layout?.panels ?? []) for (const tab of panel.tabs ?? []) found.add(tab.view);
    return found;
  }

  /**
   * The window of the main area that has, or last had, the focus.  Until
   * splits and panels are drawn (stage F3) it is the one view shown.
   */
  get mainView(): ViewModel | null {
    const windows = this.windows();
    if (windows.length === 0) return null;
    if (this.lastWindow === null || !windows.includes(this.lastWindow)) this.lastWindow = windows[0] ?? null;
    return this.lastWindow === null ? null : this.view(this.lastWindow);
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
          this.resources.clear();
          this.uiFaces = {};
          this.layout = null;
          this.views.clear();
          this.focus = null;
          this.minibuffers.clear();
          this.popup = null;
          this.keys = "";
          this.resets += 1;
          break;
        case "face_define":
          this.faces.set(a[0] as number, a[1] as FaceAttrs);
          break;
        case "regions":
          this.regions = a[0] as Record<string, RegionState>;
          break;
        case "workspaces":
          this.workspaces = a[0] as WorkspaceInfo[];
          break;
        case "status":
          this.statusLeft = a[0] as StatusItem[];
          this.statusRight = a[1] as StatusItem[];
          break;
        case "ui_faces":
          this.uiFaces = a[0] as Record<string, number>;
          break;
        case "layout": {
          const before = this.layoutViews();
          this.layout = a[0] as FrameModel["layout"];
          // A view that was in the layout and no longer is has been closed.
          const now = this.layoutViews();
          for (const id of before) if (!now.has(id)) this.views.delete(id);
          if (this.focus !== null && this.windows().includes(this.focus)) this.lastWindow = this.focus;
          break;
        }
        case "title":
          this.title = a[0] as string;
          break;
        case "focus":
          this.focus = a[0] as number;
          if (this.windows().includes(this.focus)) this.lastWindow = this.focus;
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
          const view = this.view(a[0] as number);
          view.modeline = a[1] as Run[];
          view.modelineRight = (a[2] as Run[] | undefined) ?? [];
          break;
        case "minibuffer_show":
          this.minibuffers.set(a[0] as number, { host: a[1] as Record<string, unknown>, options: a[2] as Record<string, unknown> });
          break;
        case "minibuffer_hide":
          this.minibuffers.delete(a[0] as number);
          this.views.delete(a[0] as number);
          break;
        case "popup_show":
          this.popup = { items: a[0] as [string, string][], selected: a[1] as number, view: a[2] as number, line: a[3] as number, offset: a[4] as number };
          break;
        case "popup_hide":
          this.popup = null;
          break;
        case "message": {
          const text = (a[0] as Run[]).map((run) => run[1]).join("");
          if (a[1] === "keys") this.keys = text;
          else this.message = { text, kind: a[1] as string };
          break;
        }
        case "menu":
          this.menu = a[0] as unknown[];
          break;
        case "reveal":
          this.view(a[0] as number).pendingReveal = [a[1] as number, a[2] as number, a[3] as string];
          break;
        case "resource_define":
          this.resources.set(a[0] as number, { kind: a[1] as string, source: a[2] as Record<string, unknown> });
          break;
        default:
          throw new ProtocolError(`unknown redraw event: ${name}`);
      }
    }
    return flushed;
  }
}
