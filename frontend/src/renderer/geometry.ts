// Geometry (FRONTEND 6, 7): where a buffer position is on screen, which
// position is at a given place on a visual row, and the `layout.*` requests.
//
// Everything here works from the rectangles of laid-out text, never from
// hit-testing a point, so it works for lines that are in the DOM but scrolled
// out of sight.  It assumes that along one visual row a larger offset is
// further right; bidirectional text is not handled yet.

import { cpLength, cpToUtf16 } from "./positions.ts";
import type { ViewElement } from "./view.ts";

export interface Band {
  top: number;
  bottom: number;
}

/** Where a position is, in client coordinates.  `width` is that of the character there. */
export interface Caret {
  left: number;
  top: number;
  bottom: number;
  width: number;
}

export type Position = [line: number, offset: number, goalX: number | null];

export class Geometry {
  private view: ViewElement;
  private range: Range;

  constructor(view: ViewElement) {
    this.view = view;
    this.range = document.createRange();
  }

  private content(line: number): HTMLElement | null {
    return this.view.lineElement(line)?.querySelector<HTMLElement>(".nm-content") ?? null;
  }

  /** Code points of buffer text on a line, as far as its runs show. */
  lineLength(line: number): number {
    const content = this.content(line);
    if (!content) return 0;
    let end = 0;
    for (const span of content.children) {
      const el = span as HTMLElement;
      if (el.dataset["o"] === undefined) continue;
      end = Math.max(end, Number(el.dataset["o"]) + Number(el.dataset["n"] ?? 0));
    }
    return end;
  }

  /** The rectangle of the character at a position, or of the end of the line. */
  caret(line: number, offset: number): Caret | null {
    const content = this.content(line);
    if (!content) return null;
    let last: HTMLElement | null = null;
    for (const child of content.children) {
      const span = child as HTMLElement;
      if (span.dataset["o"] === undefined) continue; // virtual text
      const start = Number(span.dataset["o"]);
      const length = Number(span.dataset["n"] ?? 0);
      last = span;
      if (offset >= start + length) continue;
      const node = span.firstChild;
      if (!(node instanceof Text) || span.classList.contains("nm-object")) {
        const r = span.getBoundingClientRect();
        return { left: r.left, top: r.top, bottom: r.bottom, width: r.width };
      }
      const within = Math.max(0, offset - start);
      const from = cpToUtf16(node.data, within);
      const to = cpToUtf16(node.data, within + 1);
      this.range.setStart(node, from);
      this.range.setEnd(node, to);
      const rects = this.range.getClientRects();
      // A character that begins a row has one rectangle there; take the last
      // if a zero-width one was left at the end of the row above.
      const r = rects[rects.length - 1] ?? span.getBoundingClientRect();
      return { left: r.left, top: r.top, bottom: r.bottom, width: r.width };
    }
    if (last !== null) {
      // Past the last character: the right edge of the last one.
      const node = last.firstChild;
      let r: DOMRect | undefined;
      if (node instanceof Text && node.data.length > 0 && !last.classList.contains("nm-object")) {
        const units = node.data.length;
        const lastStart = cpToUtf16(node.data, cpLength(node.data) - 1);
        this.range.setStart(node, lastStart);
        this.range.setEnd(node, units);
        const rects = this.range.getClientRects();
        r = rects[rects.length - 1];
      }
      r ??= last.getBoundingClientRect();
      return { left: r.right, top: r.top, bottom: r.bottom, width: 0 };
    }
    const box = content.getBoundingClientRect();
    return { left: box.left, top: box.top, bottom: box.bottom, width: 0 };
  }

  /** The visual rows of a line, top to bottom. */
  bands(line: number): Band[] {
    const content = this.content(line);
    if (!content) return [];
    this.range.selectNodeContents(content);
    const rects = [...this.range.getClientRects()].filter((r) => r.height > 0);
    if (rects.length === 0) {
      const box = content.getBoundingClientRect();
      return [{ top: box.top, bottom: box.bottom }];
    }
    rects.sort((a, b) => a.top - b.top);
    const bands: Band[] = [];
    for (const r of rects) {
      const band = bands[bands.length - 1];
      // Text of different sizes on one row overlaps vertically; rows do not.
      const overlap = band ? Math.min(band.bottom, r.bottom) - Math.max(band.top, r.top) : -1;
      if (band && overlap > 0.5 * Math.min(band.bottom - band.top, r.height)) {
        band.top = Math.min(band.top, r.top);
        band.bottom = Math.max(band.bottom, r.bottom);
      } else {
        bands.push({ top: r.top, bottom: r.bottom });
      }
    }
    return bands;
  }

  bandOf(bands: Band[], caret: Caret): number {
    const centre = (caret.top + caret.bottom) / 2;
    let best = 0;
    let distance = Infinity;
    for (let i = 0; i < bands.length; i++) {
      const band = bands[i] as Band;
      if (centre >= band.top && centre <= band.bottom) return i;
      const d = Math.min(Math.abs(centre - band.top), Math.abs(centre - band.bottom));
      if (d < distance) {
        distance = d;
        best = i;
      }
    }
    return best;
  }

  private bandAt(line: number, bands: Band[], offset: number): number {
    const caret = this.caret(line, offset);
    return caret ? this.bandOf(bands, caret) : 0;
  }

  /** The offsets on one visual row: [first, last], where `last` is the last position that stays on it. */
  rowRange(line: number, bands: Band[], band: number): [number, number] {
    const length = this.lineLength(line);
    // First offset whose row is `band` or later.
    let lo = 0;
    let hi = length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.bandAt(line, bands, mid) < band) lo = mid + 1;
      else hi = mid;
    }
    const first = lo;
    if (band >= bands.length - 1) return [first, length]; // the last row runs to the end of the line
    lo = first;
    hi = length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.bandAt(line, bands, mid) <= band) lo = mid + 1;
      else hi = mid;
    }
    return [first, Math.max(first, lo - 1)];
  }

  /** The position on a visual row whose character contains client x-coordinate `x`. */
  offsetAtX(line: number, bands: Band[], band: number, x: number): number {
    const [first, last] = this.rowRange(line, bands, band);
    let lo = first;
    let hi = last;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      const caret = this.caret(line, mid);
      if (caret && caret.left <= x + 0.5) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  private contentLeft(line: number): number {
    return this.content(line)?.getBoundingClientRect().left ?? 0;
  }

  // -- layout.* (ENGINE 4.4) ------------------------------------------------

  async move(positions: Position[], unit: string, count: number): Promise<Position[]> {
    const view = this.view;
    const results: Position[] = [];
    let rows = count;
    if (unit === "page") {
      rows = count * view.pageRows();
      view.scrollRows(rows);
    }
    for (const [line0, offset0, goal] of positions) {
      let line = Math.max(1, Math.min(line0, view.model.lineCount));
      await view.ensureLines(line, line);
      let bands = this.bands(line);
      const caret = this.caret(line, offset0);
      let band = caret ? this.bandOf(bands, caret) : 0;
      const x = goal ?? (caret ? caret.left - this.contentLeft(line) : 0);
      let left = rows;
      while (left > 0) {
        if (band + 1 < bands.length) band += 1;
        else if (line < view.model.lineCount) {
          line += 1;
          await view.ensureLines(line, line);
          bands = this.bands(line);
          band = 0;
        } else break;
        left -= 1;
      }
      while (left < 0) {
        if (band > 0) band -= 1;
        else if (line > 1) {
          line -= 1;
          await view.ensureLines(line, line);
          bands = this.bands(line);
          band = bands.length - 1;
        } else break;
        left += 1;
      }
      results.push([line, this.offsetAtX(line, bands, band, this.contentLeft(line) + x), x]);
    }
    return results;
  }

  async lineEdge(line: number, offset: number, which: string): Promise<[number, number]> {
    await this.view.ensureLines(line, line);
    const bands = this.bands(line);
    const caret = this.caret(line, offset);
    const [first, last] = this.rowRange(line, bands, caret ? this.bandOf(bands, caret) : 0);
    return [line, which === "start" ? first : last];
  }

  /** A position's rectangle relative to the view: the character's width, its row's height. */
  async rect(line: number, offset: number): Promise<{ x: number; y: number; width: number; height: number } | null> {
    await this.view.ensureLines(line, line);
    const caret = this.caret(line, offset);
    if (!caret) return null;
    const bands = this.bands(line);
    const band = bands[this.bandOf(bands, caret)] ?? { top: caret.top, bottom: caret.bottom };
    const box = this.view.scroller.getBoundingClientRect();
    return { x: caret.left - box.left, y: band.top - box.top, width: caret.width, height: band.bottom - band.top };
  }
}
