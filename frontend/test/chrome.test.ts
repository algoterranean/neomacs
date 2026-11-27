import assert from "node:assert/strict";
import { test } from "node:test";
import { accelerator } from "../src/main/accelerator.ts";
import { FrameModel } from "../src/renderer/model.ts";

test("a single chord becomes an accelerator; a sequence does not", () => {
  assert.equal(accelerator("s-o", true), "Cmd+O");
  assert.equal(accelerator("s-o", false), "Super+O");
  assert.equal(accelerator("C-M-x", true), "Ctrl+Alt+X");
  assert.equal(accelerator("s-Z", true), "Cmd+Shift+Z");
  assert.equal(accelerator("M-;", true), "Alt+;");
  assert.equal(accelerator("<f9>", true), "F9");
  assert.equal(accelerator("S-<f11>", true), "Shift+F11");
  assert.equal(accelerator("C-<return>", false), "Ctrl+Return");
  assert.equal(accelerator("C-x C-f", true), null);
  assert.equal(accelerator("", true), null);
  assert.equal(accelerator(undefined, true), null);
  assert.equal(accelerator("<mouse-1>", true), null);
});

test("the window that has, or last had, the focus is the main view", () => {
  const frame = new FrameModel();
  frame.apply([
    ["layout", { main: ["split", 9, "h", [0.5, 0.5], [["win", 1], ["split", 8, "v", [0.5, 0.5], [["win", 2], ["win", 3]]]]], panels: [
      { id: 1, name: "bottom", visible: true, mode: "docked", side: "bottom", size: 0.3, tabs: [{ view: 7, title: "a" }], active: 7 },
    ] }],
    ["focus", 3],
  ]);
  assert.deepEqual(frame.windows(), [1, 2, 3]);
  assert.deepEqual([...frame.layoutViews()].sort(), [1, 2, 3, 7]);
  assert.equal(frame.mainView?.id, 3);
  frame.apply([["focus", 7]]); // a panel has the keyboard: the main view is still the last window
  assert.equal(frame.mainView?.id, 3);
});

test("a view that leaves the layout is dropped, and its lines with it", () => {
  const frame = new FrameModel();
  frame.apply([
    ["layout", { main: ["split", 9, "v", [0.5, 0.5], [["win", 1], ["win", 2]]], panels: [] }],
    ["lines", 2, 1, 1, [[{}, [[0, "gone soon", 0]]]]],
    ["focus", 2],
  ]);
  assert.ok(frame.views.has(2));
  frame.apply([["layout", { main: ["win", 1], panels: [] }]]);
  assert.equal(frame.views.has(2), false);
  assert.equal(frame.mainView?.id, 1);
});

test("resources are kept by id and forgotten on reset", () => {
  const frame = new FrameModel();
  frame.apply([["resource_define", 4, "image", { path: "/tmp/x.png" }]]);
  assert.deepEqual(frame.resources.get(4), { kind: "image", source: { path: "/tmp/x.png" } });
  frame.apply([["reset"]]);
  assert.equal(frame.resources.size, 0);
});
