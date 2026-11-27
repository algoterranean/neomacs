import assert from "node:assert/strict";
import { test } from "node:test";
import { type KeyEventLike, translateKey } from "../src/renderer/keys.ts";
import { cpLength, cpToUtf16, isBmp, utf16ToCp } from "../src/renderer/positions.ts";

const mac = { mac: true, optionIsMeta: true };
const pc = { mac: false, optionIsMeta: true };

function ev(key: string, code: string, mods = ""): KeyEventLike {
  return {
    key,
    code,
    ctrlKey: mods.includes("C"),
    altKey: mods.includes("M"),
    metaKey: mods.includes("s"),
    shiftKey: mods.includes("S"),
  };
}

test("plain and shifted characters", () => {
  assert.equal(translateKey(ev("a", "KeyA"), mac), "a");
  assert.equal(translateKey(ev("A", "KeyA", "S"), mac), "A"); // shift is in the character
  assert.equal(translateKey(ev("<", "Comma", "S"), mac), "<");
  assert.equal(translateKey(ev(" ", "Space"), mac), "<space>");
  assert.equal(translateKey(ev("é", "KeyE"), mac), "é");
  assert.equal(translateKey(ev("👍", ""), mac), "👍");
});

test("named keys keep shift", () => {
  assert.equal(translateKey(ev("Enter", "Enter"), mac), "<return>");
  assert.equal(translateKey(ev("ArrowLeft", "ArrowLeft", "S"), mac), "S-<left>");
  assert.equal(translateKey(ev("ArrowDown", "ArrowDown", "CM"), mac), "C-M-<down>");
  assert.equal(translateKey(ev("PageUp", "PageUp"), mac), "<prior>");
  assert.equal(translateKey(ev("F5", "F5"), mac), "<f5>");
  assert.equal(translateKey(ev("Backspace", "Backspace", "M"), mac), "M-<backspace>");
  assert.equal(translateKey(ev("Tab", "Tab", "S"), mac), "S-<tab>");
  assert.equal(translateKey(ev(" ", "Space", "C"), mac), "C-<space>");
});

test("control and meta go by the physical key", () => {
  assert.equal(translateKey(ev("\u0018", "KeyX", "C"), mac), "C-x"); // Control makes key a control character
  assert.equal(translateKey(ev("ƒ", "KeyF", "M"), mac), "M-f"); // Option-f types ƒ on macOS
  assert.equal(translateKey(ev("¯", "Comma", "MS"), mac), "M-<");
  assert.equal(translateKey(ev("≥", "Period", "MS"), mac), "M->");
  assert.equal(translateKey(ev("n", "KeyN", "CM"), mac), "C-M-n");
  assert.equal(translateKey(ev("-", "Minus", "M"), mac), "M--");
  assert.equal(translateKey(ev("5", "Digit5", "M"), mac), "M-5");
  assert.equal(translateKey(ev("/", "Slash", "C"), mac), "C-/");
  assert.equal(translateKey(ev("_", "Minus", "CS"), mac), "C-_");
  assert.equal(translateKey(ev("x", "KeyX", "M"), pc), "M-x");
});

test("command is super", () => {
  assert.equal(translateKey(ev("v", "KeyV", "s"), mac), "s-v");
  assert.equal(translateKey(ev("Z", "KeyZ", "sS"), mac), "s-Z");
  assert.equal(translateKey(ev("ArrowLeft", "ArrowLeft", "s"), mac), "s-<left>");
});

test("option can be left to type characters", () => {
  const typing = { mac: true, optionIsMeta: false };
  assert.equal(translateKey(ev("ƒ", "KeyF", "M"), typing), "f"); // falls back to the physical key
  assert.equal(translateKey(ev("x", "KeyX", "M"), { mac: false, optionIsMeta: false }), "M-x"); // Alt elsewhere is always Meta
});

test("events that are not key presses to send", () => {
  for (const key of ["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead", "Unidentified"]) {
    assert.equal(translateKey(ev(key, ""), mac), null);
  }
  assert.equal(translateKey({ ...ev("a", "KeyA"), isComposing: true }, mac), null);
  assert.equal(translateKey({ ...ev("Process", "KeyA"), keyCode: 229 }, mac), null);
  assert.equal(translateKey(ev("MediaPlayPause", ""), mac), null);
});

test("code points and UTF-16", () => {
  const text = "a👍b中👨‍👩‍👧c";
  assert.equal(isBmp("héllo 中文"), true);
  assert.equal(isBmp(text), false);
  assert.equal(cpLength(text), [...text].length);
  for (let cp = 0; cp <= cpLength(text); cp++) {
    const index = cpToUtf16(text, cp);
    assert.equal(utf16ToCp(text, index), cp);
    assert.equal([...text.slice(0, index)].length, cp);
  }
  assert.equal(cpToUtf16(text, 2), 3); // past the surrogate pair
  assert.equal(cpToUtf16("abc", 99), 3);
  assert.equal(utf16ToCp("abc", 2), 2);
  assert.equal(cpToUtf16("", 0), 0);
});
