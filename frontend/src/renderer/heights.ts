// The height index (FRONTEND 4, 5.4): where each line is, in pixels.
//
// Lines that have been laid out have a measured height.  Every other line is
// assumed to have the estimate.  Measured heights are kept only for the held
// range, in step with the model's array of held lines.

import type { LineObserver, ViewModel } from "./model.ts";

export class HeightIndex implements LineObserver {
  private view: ViewModel;
  private heights: number[] = []; // per held line; NaN until measured
  private prefix: number[] | null = null; // prefix[i] = pixels above held line i
  private measuredSum = 0;
  private measuredCount = 0;
  estimate: number;

  constructor(view: ViewModel, estimate: number) {
    this.view = view;
    this.estimate = estimate;
    this.replaced();
  }

  // -- LineObserver ---------------------------------------------------------

  spliced(index: number, removed: number, added: number): void {
    const gone = this.heights.splice(index, removed, ...new Array<number>(added).fill(NaN));
    for (const h of gone) this.forget(h);
    this.prefix = null;
  }

  replaced(): void {
    this.heights = new Array<number>(this.view.lines.length).fill(NaN);
    this.prefix = null;
  }

  private forget(height: number): void {
    if (!Number.isNaN(height)) {
      this.measuredSum -= height;
      this.measuredCount -= 1;
    }
  }

  // -- measuring ------------------------------------------------------------

  /** Record the measured height of a held line.  Returns true if it changed. */
  measure(line: number, height: number): boolean {
    const i = line - this.view.first;
    const old = this.heights[i];
    if (old === undefined || old === height) return false;
    this.forget(old);
    this.heights[i] = height;
    this.measuredSum += height;
    this.measuredCount += 1;
    this.prefix = null;
    return true;
  }

  /** Forget every measurement, e.g. after the width or the fonts changed. */
  invalidate(): void {
    this.replaced();
    this.measuredSum = 0;
    this.measuredCount = 0;
  }

  /** Use the average of what has been measured as the height of unmeasured lines. */
  refineEstimate(): void {
    if (this.measuredCount >= 8) {
      const average = this.measuredSum / this.measuredCount;
      if (Math.abs(average - this.estimate) > 0.5) {
        this.estimate = average;
        this.prefix = null;
      }
    }
  }

  isMeasured(line: number): boolean {
    const h = this.heights[line - this.view.first];
    return h !== undefined && !Number.isNaN(h);
  }

  // -- queries --------------------------------------------------------------

  private sums(): number[] {
    if (this.prefix === null) {
      const prefix = new Array<number>(this.heights.length + 1);
      let y = 0;
      for (let i = 0; i < this.heights.length; i++) {
        prefix[i] = y;
        const h = this.heights[i] as number;
        y += Number.isNaN(h) ? this.estimate : h;
      }
      prefix[this.heights.length] = y;
      this.prefix = prefix;
    }
    return this.prefix;
  }

  height(line: number): number {
    const h = this.heights[line - this.view.first];
    return h === undefined || Number.isNaN(h) ? this.estimate : h;
  }

  /** Pixels above the top of `line` (1-based).  `line` may be lineCount + 1. */
  top(line: number): number {
    const { first, lines } = this.view;
    if (lines.length === 0 || line <= first) return (line - 1) * this.estimate;
    const sums = this.sums();
    const held = sums[lines.length] as number;
    const base = (first - 1) * this.estimate;
    const i = line - first;
    if (i <= lines.length) return base + (sums[i] as number);
    return base + held + (i - lines.length) * this.estimate;
  }

  get total(): number {
    return this.top(this.view.lineCount + 1);
  }

  /** The line containing vertical position `y`, and how far into it `y` is. */
  lineAt(y: number): { line: number; offset: number } {
    const count = Math.max(1, this.view.lineCount);
    const { first, lines } = this.view;
    const clamp = (line: number) => Math.max(1, Math.min(count, line));
    if (y <= 0) return { line: 1, offset: 0 };
    const base = (first - 1) * this.estimate;
    let line: number;
    if (lines.length === 0 || y < base) {
      line = clamp(Math.floor(y / this.estimate) + 1);
    } else {
      const sums = this.sums();
      const held = sums[lines.length] as number;
      if (y >= base + held) {
        line = clamp(first + lines.length + Math.floor((y - base - held) / this.estimate));
      } else {
        // Binary search for the last held line whose top is at or above y.
        let lo = 0;
        let hi = lines.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          if (base + (sums[mid] as number) <= y) lo = mid;
          else hi = mid - 1;
        }
        line = clamp(first + lo);
      }
    }
    return { line, offset: Math.max(0, y - this.top(line)) };
  }
}
