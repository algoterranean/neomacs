// Code points and UTF-16 (FRONTEND 6).  The engine counts code points; the DOM
// and JavaScript strings count UTF-16 units.

const SURROGATE = /[\uD800-\uDFFF]/;

/** True if the two ways of counting agree for `text`. */
export function isBmp(text: string): boolean {
  return !SURROGATE.test(text);
}

/** How many code points `text` has. */
export function cpLength(text: string): number {
  if (isBmp(text)) return text.length;
  let count = 0;
  for (const _ of text) count += 1;
  return count;
}

/** The UTF-16 index of code point `cp` in `text`. */
export function cpToUtf16(text: string, cp: number): number {
  if (cp <= 0) return 0;
  if (isBmp(text)) return Math.min(cp, text.length);
  let index = 0;
  for (let seen = 0; seen < cp && index < text.length; seen++) {
    const unit = text.charCodeAt(index);
    index += unit >= 0xd800 && unit <= 0xdbff && index + 1 < text.length ? 2 : 1;
  }
  return index;
}

/** The code point offset of UTF-16 index `index` in `text`. */
export function utf16ToCp(text: string, index: number): number {
  if (index <= 0) return 0;
  if (isBmp(text)) return Math.min(index, text.length);
  let cp = 0;
  let at = 0;
  const end = Math.min(index, text.length);
  while (at < end) {
    const unit = text.charCodeAt(at);
    at += unit >= 0xd800 && unit <= 0xdbff && at + 1 < text.length ? 2 : 1;
    cp += 1;
  }
  return cp;
}
