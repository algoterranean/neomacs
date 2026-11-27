// Faces as generated CSS rules (FRONTEND 5.2).  One rule per face id; a theme
// change rewrites rules and touches no element.

import type { FaceAttrs } from "./model.ts";

const COLOUR = /^(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)$/;

function colour(value: unknown): string | null {
  return typeof value === "string" && COLOUR.test(value) ? value : null;
}

/** The declarations for a face.  Nothing from the engine reaches the sheet unchecked. */
export function declarations(attrs: FaceAttrs): string {
  const out: string[] = [];
  const fg = colour(attrs["fg"]);
  const bg = colour(attrs["bg"]);
  if (fg) out.push(`color: ${fg}`);
  if (bg) out.push(`background-color: ${bg}`);
  const family = attrs["family"];
  if (typeof family === "string" && family.length > 0) {
    const names = family
      .split(",")
      .map((name) => name.replace(/[^\p{L}\p{N} _-]/gu, "").trim())
      .filter((name) => name.length > 0)
      .map((name) => `"${name}"`);
    if (names.length > 0) out.push(`font-family: ${names.join(", ")}, sans-serif`);
  }
  // A number is a multiple of the surrounding size; "12pt" is an absolute size.
  const size = attrs["size"];
  if (typeof size === "number" && size > 0 && size < 20) out.push(`font-size: ${size}em`);
  else if (typeof size === "string" && /^\d+(\.\d+)?pt$/.test(size)) out.push(`font-size: ${size}`);
  const weight = attrs["weight"];
  if (weight === "bold" || weight === "normal" || weight === "light") out.push(`font-weight: ${weight === "light" ? 300 : weight}`);
  else if (typeof weight === "number" && weight >= 1 && weight <= 1000) out.push(`font-weight: ${weight}`);
  const slant = attrs["slant"];
  if (slant === "italic" || slant === "oblique" || slant === "normal") out.push(`font-style: ${slant}`);
  const lines: string[] = [];
  if (attrs["underline"]) lines.push("underline");
  if (attrs["strike"]) lines.push("line-through");
  if (lines.length > 0) out.push(`text-decoration-line: ${lines.join(" ")}`);
  const underline = colour(attrs["underline"]);
  if (underline) out.push(`text-decoration-color: ${underline}`);
  const box = colour(attrs["box"]);
  if (attrs["box"]) out.push(`outline: 1px solid ${box ?? "currentColor"}`, "outline-offset: -1px");
  const lineHeight = attrs["line_height"];
  if (typeof lineHeight === "number" && lineHeight > 0 && lineHeight < 10) out.push(`line-height: ${lineHeight}`);
  return out.join("; ");
}

export function selector(id: number): string {
  return id === 0 ? ".nm-frame" : `.f${id}`;
}

export class FaceSheet {
  private sheet: CSSStyleSheet;
  private index = new Map<number, number>();

  constructor(doc: Document) {
    this.sheet = new CSSStyleSheet();
    doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, this.sheet];
  }

  define(id: number, attrs: FaceAttrs): void {
    const rule = `${selector(id)} { ${declarations(attrs)} }`;
    const at = this.index.get(id);
    if (at === undefined) {
      this.index.set(id, this.sheet.insertRule(rule, this.sheet.cssRules.length));
    } else {
      this.sheet.deleteRule(at);
      this.sheet.insertRule(rule, at);
    }
  }

  clear(): void {
    while (this.sheet.cssRules.length > 0) this.sheet.deleteRule(0);
    this.index.clear();
  }
}
