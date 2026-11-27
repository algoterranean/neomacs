// Timings of the renderer's own work, against the budgets of FRONTEND 11.
//
// What is timed is the script's work on the main thread.  Painting happens
// afterwards, in the compositor, where a page cannot time it; "layout" below
// includes the style and layout work, which is forced before the clock stops.

/** The budgets, in milliseconds (FRONTEND 11). */
export const BUDGETS: Record<string, number> = {
  apply: 1, // parse and apply a typical keystroke batch
  render: 6, // layout for that batch
  total: 8, // everything the renderer does for one keystroke
  move: 2, // answer `layout.move` for one position
  scroll: 8, // a scroll frame with all lines held
};

const LIMIT = 5000;

export function percentile(values: number[], q: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] as number;
}

export interface Summary {
  n: number;
  p50: number;
  p90: number;
  max: number;
}

export function summarise(values: number[]): Summary {
  const round = (ms: number) => Math.round(ms * 1000) / 1000;
  return {
    n: values.length,
    p50: round(percentile(values, 0.5)),
    p90: round(percentile(values, 0.9)),
    max: round(values.reduce((a, b) => Math.max(a, b), 0)),
  };
}

/** The phases over budget: each with its 90th percentile and what it is allowed. */
export function overBudget(summary: Record<string, Summary>, scale = 1): Record<string, [number, number]> {
  const over: Record<string, [number, number]> = {};
  for (const [name, budget] of Object.entries(BUDGETS)) {
    const found = summary[name];
    if (found && found.n > 0 && found.p90 > budget * scale) over[name] = [found.p90, budget * scale];
  }
  return over;
}

export class Perf {
  private samples = new Map<string, number[]>();
  /** Work done for the keystroke in hand, before its batch has been drawn. */
  private keyWork: number | null = null;
  /** Set when the user scrolled: the next render is a scroll frame. */
  scrolled = false;

  add(name: string, ms: number): void {
    let list = this.samples.get(name);
    if (!list) this.samples.set(name, (list = []));
    if (list.length < LIMIT) list.push(ms);
  }

  /** A key event has been translated and sent; MS is what that took. */
  key(ms: number): void {
    this.keyWork = ms;
  }

  /** A batch has been applied; MS is what that took. */
  applied(ms: number): void {
    this.add("apply", ms);
    if (this.keyWork !== null) this.keyWork += ms;
  }

  /** The page has been brought up to date; MS is what that took. */
  rendered(ms: number, hadBatch: boolean): void {
    if (hadBatch) {
      this.add("render", ms);
      if (this.keyWork !== null) {
        this.add("total", this.keyWork + ms);
        this.keyWork = null;
      }
    }
    // A frame drawn because the user scrolled, whether or not the engine had
    // something to say in the same frame (it usually has: lines newly in view).
    if (this.scrolled) this.add("scroll", ms);
    this.scrolled = false;
  }

  reset(): void {
    this.samples.clear();
    this.keyWork = null;
    this.scrolled = false;
  }

  summary(): Record<string, Summary> {
    const out: Record<string, Summary> = {};
    for (const [name, values] of this.samples) out[name] = summarise(values);
    return out;
  }
}
