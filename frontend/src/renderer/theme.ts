// The colours of what the frontend draws around the text (FRONTEND 5.2).
//
// The engine names a face for each (`ui_faces`), and the stylesheet reads them
// as custom properties, so a change of theme changes the chrome as it changes
// the text: by rewriting rules, with no element touched.

import { colour } from "./faces.ts";
import type { FaceAttrs } from "./model.ts";

// Custom property -> [the face it comes from, which of its colours].  The
// face "default" is face 0.
const SOURCES: [string, string, "fg" | "bg"][] = [
  ["--nm-bg", "default", "bg"],
  ["--nm-fg", "default", "fg"],
  ["--nm-surface", "popup", "bg"],
  ["--nm-surface-fg", "popup", "fg"],
  ["--nm-selected", "popup-selected", "bg"],
  ["--nm-raised", "toast", "bg"],
  ["--nm-border", "chrome-border", "fg"],
  ["--nm-divider", "divider", "fg"],
  ["--nm-accent", "accent", "fg"],
  ["--nm-scroll", "scrollbar", "fg"],
  ["--nm-error", "error", "fg"],
  ["--nm-warning", "warning", "fg"],
  ["--nm-meta", "metabar", "bg"],
  ["--nm-meta-fg", "metabar", "fg"],
  ["--nm-sidebar", "sidebar", "bg"],
  ["--nm-sidebar-fg", "sidebar", "fg"],
  ["--nm-status", "statusbar", "bg"],
  ["--nm-status-fg", "statusbar", "fg"],
];

/** Whether a colour is nearer white than black.  Only #rgb and #rrggbb are judged. */
export function isLight(value: string): boolean {
  let hex = value.startsWith("#") ? value.slice(1) : "";
  if (hex.length === 3 || hex.length === 4) hex = [...hex.slice(0, 3)].map((c) => c + c).join("");
  if (hex.length !== 6 && hex.length !== 8) return false;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 140;
}

/** The custom properties for these faces.  A colour that is not one is left out. */
export function themeVariables(faces: Map<number, FaceAttrs>, uiFaces: Record<string, number>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [property, face, which] of SOURCES) {
    const id = face === "default" ? 0 : uiFaces[face];
    if (id === undefined) continue;
    const value = colour(faces.get(id)?.[which]);
    if (value) out[property] = value;
  }
  const background = out["--nm-bg"];
  if (background) out["color-scheme"] = isLight(background) ? "light" : "dark";
  return out;
}

export function applyTheme(target: HTMLElement, variables: Record<string, string>): void {
  for (const [property] of SOURCES) {
    const value = variables[property];
    if (value) target.style.setProperty(property, value);
    else target.style.removeProperty(property);
  }
  target.style.setProperty("color-scheme", variables["color-scheme"] ?? "dark");
}
