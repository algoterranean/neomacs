import assert from "node:assert/strict";
import { test } from "node:test";
import { HeightIndex } from "../src/renderer/heights.ts";
import { type Line, ViewModel } from "../src/renderer/model.ts";

const lines = (n: number): Line[] => Array.from({ length: n }, () => [{}, []] as Line);

function setup(count: number, first: number, held: number, estimate = 10) {
  const view = new ViewModel(1);
  view.info = { line_count: count, wrap: true, tab_width: 8, gutter: [] };
  const index = new HeightIndex(view, estimate);
  view.observer = index;
  view.splice(first, held, lines(held));
  return { view, index };
}

test("unmeasured lines use the estimate", () => {
  const { index } = setup(1000, 101, 50);
  assert.equal(index.top(1), 0);
  assert.equal(index.top(101), 1000);
  assert.equal(index.top(500), 4990);
  assert.equal(index.total, 10000);
  assert.deepEqual(index.lineAt(0), { line: 1, offset: 0 });
  assert.deepEqual(index.lineAt(1005), { line: 101, offset: 5 });
  assert.deepEqual(index.lineAt(4995), { line: 500, offset: 5 });
  assert.equal(index.lineAt(1e9).line, 1000);
});

test("measured heights move everything below them", () => {
  const { index } = setup(1000, 101, 50);
  assert.equal(index.measure(101, 30), true);
  assert.equal(index.measure(101, 30), false);
  assert.equal(index.measure(110, 25), true);
  assert.equal(index.measure(5, 99), false); // not held: ignored
  assert.equal(index.height(101), 30);
  assert.equal(index.top(102), 1030);
  assert.equal(index.top(111), 1030 + 8 * 10 + 25);
  assert.equal(index.top(151), 1000 + 30 + 25 + 48 * 10);
  assert.equal(index.total, 10000 + 20 + 15);
  assert.deepEqual(index.lineAt(1029), { line: 101, offset: 29 });
  assert.deepEqual(index.lineAt(1030), { line: 102, offset: 0 });
  assert.deepEqual(index.lineAt(index.top(600) + 3), { line: 600, offset: 3 });
  assert.ok(index.isMeasured(101) && !index.isMeasured(102));
});

test("heights follow splices of the held lines", () => {
  const { view, index } = setup(100, 1, 10);
  for (let n = 1; n <= 10; n++) index.measure(n, 10 + n);
  view.splice(3, 2, lines(1)); // lines 3 and 4 replaced by one unmeasured line
  view.info.line_count = 99;
  assert.equal(index.height(1), 11);
  assert.equal(index.height(3), 10); // the estimate again
  assert.equal(index.height(4), 15); // what was line 5
  view.splice(10, 5, lines(5)); // a fill below: nine lines were held
  assert.equal(index.height(9), 20); // what was line 10
  assert.equal(index.height(12), 10);
  view.hold(4, 6);
  assert.equal(index.height(4), 10); // dropped measurements are forgotten
});

test("the estimate becomes the average of what was measured", () => {
  const { index } = setup(1000, 1, 20);
  for (let n = 1; n <= 20; n++) index.measure(n, 18);
  index.refineEstimate();
  assert.equal(index.estimate, 18);
  assert.equal(index.total, 18000);
  index.invalidate();
  assert.equal(index.isMeasured(1), false);
});
