import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { FrameModel, type Line, ProtocolError, type RedrawEvent, ViewModel } from "../src/renderer/model.ts";

const line = (text: string): Line => [{}, [[0, text, 0]]];
const texts = (v: ViewModel) => v.lines.map((l) => l[1].map((r) => r[1]).join(""));

test("the recorded session replays to the recorded state", () => {
  // The same file the Python frontends replay (ENGINE 14, FRONTEND 12).
  const file = path.join(import.meta.dirname, "../../tests/data/sessions/basic.json");
  const recording = JSON.parse(readFileSync(file, "utf8")) as {
    protocol: number;
    batches: RedrawEvent[][];
    final: {
      faces: Record<string, unknown>;
      ui_faces: Record<string, number>;
      layout: unknown;
      focus: number;
      title: string;
      views: Record<string, { info: unknown; first: number; lines: Line[]; selections: unknown; modeline: unknown; shape: string }>;
    };
  };
  assert.equal(recording.protocol, 1);
  const model = new FrameModel();
  const seqs: number[] = [];
  for (const batch of recording.batches) seqs.push(...model.apply(batch));
  assert.equal(seqs.length, recording.batches.length);
  assert.deepEqual([...seqs].sort((a, b) => a - b), seqs);
  const faces = Object.fromEntries([...model.faces.entries()].sort((a, b) => a[0] - b[0]).map(([k, v]) => [String(k), v]));
  assert.deepEqual(faces, recording.final.faces);
  assert.deepEqual(model.uiFaces, recording.final.ui_faces);
  assert.deepEqual(model.layout, recording.final.layout);
  assert.equal(model.focus, recording.final.focus);
  assert.equal(model.title, recording.final.title);
  for (const [id, expected] of Object.entries(recording.final.views)) {
    const view = model.views.get(Number(id));
    assert.ok(view);
    assert.deepEqual(view.info, expected.info);
    assert.equal(view.first, expected.first);
    assert.deepEqual(view.lines, expected.lines);
    assert.deepEqual(view.selections, expected.selections);
    assert.deepEqual(view.modeline, expected.modeline);
    assert.equal(view.shape, expected.shape);
  }
});

test("splices follow the three rules for what is held", () => {
  const v = new ViewModel(1);
  v.splice(10, 3, [line("a"), line("b"), line("c")]);
  assert.deepEqual([v.first, v.last], [10, 12]);
  v.splice(11, 1, [line("B1"), line("B2")]);
  assert.deepEqual(texts(v), ["a", "B1", "B2", "c"]);
  v.splice(14, 2, [line("d"), line("e")]); // fill just below
  assert.deepEqual([v.first, v.last], [10, 15]);
  v.splice(8, 2, [line("y"), line("z")]); // fill just above
  assert.deepEqual([v.first, v.last, texts(v)[0]], [8, 15, "y"]);
  v.splice(2, 1, 4); // a count above: renumber
  assert.deepEqual([v.first, v.last], [11, 18]);
  v.splice(30, 5, 0); // a count below: nothing
  assert.deepEqual([v.first, v.last], [11, 18]);
  v.splice(9, 4, [line("X")]); // starts above and reaches in
  assert.deepEqual([v.first, v.last, texts(v).slice(0, 2)], [9, 15, ["X", "a"]]);
  v.hold(10, 12);
  assert.deepEqual([v.first, texts(v)], [10, ["a", "B1", "B2"]]);
  assert.throws(() => v.splice(20, 0, [line("gap")]), ProtocolError);
  assert.throws(() => v.splice(5, 0, [line("gap")]), ProtocolError);
  v.hold(1, 0);
  assert.equal(v.lines.length, 0);
});

test("the scroll position stays on the same text", () => {
  const v = new ViewModel(1);
  v.splice(1, 20, Array.from({ length: 20 }, (_, i) => line(String(i + 1))));
  v.topLine = 10;
  v.topOffset = 5;
  v.splice(3, 0, [line("new")]);
  assert.deepEqual([v.topLine, v.topOffset], [11, 5]);
  v.splice(1, 2, 0);
  assert.equal(v.topLine, 9);
  v.splice(15, 1, [line("below")]);
  assert.deepEqual([v.topLine, v.topOffset], [9, 5]);
  v.splice(9, 1, [line("replaced")]);
  assert.deepEqual([v.topLine, v.topOffset], [9, 0]);
});

test("observers hear about every change to the held lines", () => {
  const v = new ViewModel(1);
  const seen: string[] = [];
  v.observer = { spliced: (i, r, a) => seen.push(`splice ${i} ${r} ${a}`), replaced: () => seen.push("replaced") };
  v.splice(5, 2, [line("a"), line("b")]);
  v.splice(5, 1, [line("x"), line("y")]);
  v.splice(3, 2, [line("p"), line("q")]);
  v.splice(7, 3, [line("z")]); // extends past the end: only what was held is removed
  v.splice(1, 1, 5);
  v.hold(8, 9);
  assert.deepEqual(seen, ["replaced", "splice 0 1 2", "splice 0 0 2", "splice 4 1 1", "replaced"]);
});

test("unknown events are refused", () => {
  assert.throws(() => new FrameModel().apply([["mystery", 1]]), ProtocolError);
});
