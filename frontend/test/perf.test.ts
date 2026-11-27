import assert from "node:assert/strict";
import { test } from "node:test";
import { BUDGETS, overBudget, Perf, percentile, summarise } from "../src/renderer/perf.ts";
import { isLight, themeVariables } from "../src/renderer/theme.ts";

test("percentiles and summaries", () => {
  assert.equal(percentile([], 0.9), 0);
  assert.equal(percentile([5, 1, 3], 0.5), 3);
  const values = Array.from({ length: 100 }, (_, i) => i + 1);
  assert.deepEqual(summarise(values), { n: 100, p50: 51, p90: 91, max: 100 });
});

test("a keystroke's total is the work for the key, its batch and its render", () => {
  const perf = new Perf();
  perf.key(0.25);
  perf.applied(0.5);
  perf.rendered(2, true);
  // A batch nobody typed for (a timer's, say) counts as a batch but not as a keystroke.
  perf.applied(0.5);
  perf.rendered(3, true);
  const s = perf.summary();
  assert.deepEqual([s["apply"]?.n, s["render"]?.n, s["total"]?.n], [2, 2, 1]);
  assert.equal(s["total"]?.max, 2.75);
});

test("a render after the user scrolled is a scroll frame; any other with no batch is not counted", () => {
  const perf = new Perf();
  perf.rendered(4, false);
  perf.scrolled = true;
  perf.rendered(5, false);
  perf.rendered(6, false);
  assert.deepEqual(perf.summary(), { scroll: { n: 1, p50: 5, p90: 5, max: 5 } });
  // The engine often has lines to send in the same frame: still a scroll frame.
  perf.scrolled = true;
  perf.applied(0.1);
  perf.rendered(7, true);
  assert.equal(perf.summary()["scroll"]?.n, 2);
  assert.equal(perf.summary()["render"]?.n, 1);
  perf.reset();
  assert.deepEqual(perf.summary(), {});
});

test("what is over budget is named, with its figure and its allowance", () => {
  const perf = new Perf();
  for (let i = 0; i < 20; i++) {
    perf.key(0.1);
    perf.applied(0.4);
    perf.rendered(7, true);
    perf.add("move", 1);
  }
  const summary = perf.summary();
  assert.deepEqual(overBudget(summary), { render: [7, BUDGETS["render"]] });
  assert.deepEqual(overBudget(summary, 3), {}); // a slower machine is allowed more
  assert.deepEqual(overBudget({}), {}); // what was not measured is not judged
});

test("the chrome's colours come from the faces the engine names", () => {
  const faces = new Map<number, Record<string, unknown>>([
    [0, { fg: "#1f2328", bg: "#ffffff" }],
    [7, { fg: "#1f2328", bg: "#f6f8fa" }],
    [8, { bg: "#b6d7ff" }],
    [9, { fg: "url(javascript:alert(1))" }],
  ]);
  const vars = themeVariables(faces, { popup: 7, "popup-selected": 8, error: 9, accent: 99 });
  assert.deepEqual(vars, {
    "--nm-bg": "#ffffff",
    "--nm-fg": "#1f2328",
    "--nm-surface": "#f6f8fa",
    "--nm-surface-fg": "#1f2328",
    "--nm-selected": "#b6d7ff",
    "color-scheme": "light",
  }); // a value that is not a colour, and a face never defined, give nothing
  assert.equal(themeVariables(new Map([[0, { bg: "#1e1e1e" }]]), {})["color-scheme"], "dark");
});

test("light and dark", () => {
  assert.equal(isLight("#ffffff"), true);
  assert.equal(isLight("#fff"), true);
  assert.equal(isLight("#1e1e1e"), false);
  assert.equal(isLight("#000"), false);
  assert.equal(isLight("white"), false); // a named colour is not judged
});
