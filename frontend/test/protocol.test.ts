import assert from "node:assert/strict";
import { test } from "node:test";
import { declarations, selector } from "../src/renderer/faces.ts";
import { RemoteError, Rpc } from "../src/renderer/protocol.ts";

test("requests, replies and errors", async () => {
  const sent: string[] = [];
  const rpc = new Rpc((line) => sent.push(line));
  const first = rpc.call("engine.eval", "1 + 1");
  const second = rpc.call("nope");
  assert.deepEqual(JSON.parse(sent[0] as string), { jsonrpc: "2.0", id: 1, method: "engine.eval", params: ["1 + 1"] });
  rpc.handleLine('{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"method not found"}}');
  rpc.handleLine('{"jsonrpc":"2.0","id":1,"result":"2"}');
  rpc.handleLine('{"jsonrpc":"2.0","id":99,"result":"stale"}'); // from before a reload: ignored
  rpc.handleLine("not json");
  assert.equal(await first, "2");
  await assert.rejects(second, (error: unknown) => error instanceof RemoteError && error.code === -32601);
});

test("notifications and requests from the engine", async () => {
  const sent: string[] = [];
  const rpc = new Rpc((line) => sent.push(line));
  const seen: unknown[] = [];
  rpc.onNotification("redraw", (events) => seen.push(events));
  rpc.onRequest("layout.viewport", (view) => ({ first: 1, last: 2, view }));
  rpc.onRequest("layout.broken", () => {
    throw new Error("boom");
  });
  rpc.handleLine('{"jsonrpc":"2.0","method":"redraw","params":[[["flush",1]]]}');
  assert.deepEqual(seen, [[["flush", 1]]]);
  rpc.handleLine('{"jsonrpc":"2.0","id":7,"method":"layout.viewport","params":[3]}');
  rpc.handleLine('{"jsonrpc":"2.0","id":8,"method":"layout.unknown","params":[]}');
  rpc.handleLine('{"jsonrpc":"2.0","id":9,"method":"layout.broken","params":[]}');
  await new Promise((resolve) => setTimeout(resolve, 0));
  const replies = sent.map((line) => JSON.parse(line) as { id: number; result?: unknown; error?: { code: number } });
  assert.deepEqual(replies.find((r) => r.id === 7), { jsonrpc: "2.0", id: 7, result: { first: 1, last: 2, view: 3 } });
  assert.equal(replies.find((r) => r.id === 8)?.error?.code, -32601);
  assert.equal(replies.find((r) => r.id === 9)?.error?.code, -32000);
  rpc.notify("view.ack", 1);
  assert.deepEqual(JSON.parse(sent.at(-1) as string), { jsonrpc: "2.0", method: "view.ack", params: [1] });
});

test("abandon rejects what was waiting", async () => {
  const rpc = new Rpc(() => {});
  const waiting = rpc.call("slow");
  rpc.abandon("reconnected");
  await assert.rejects(waiting, /reconnected/);
});

test("face declarations", () => {
  assert.equal(selector(0), ".nm-frame");
  assert.equal(selector(7), ".f7");
  assert.equal(declarations({ fg: "#c586c0", bg: "#1e1e1e" }), "color: #c586c0; background-color: #1e1e1e");
  assert.equal(
    declarations({ size: 1.6, weight: "bold", slant: "italic", underline: true, strike: true, line_height: 1.5 }),
    "font-size: 1.6em; font-weight: bold; font-style: italic; text-decoration-line: underline line-through; line-height: 1.5",
  );
  assert.equal(declarations({ family: "Inter, Helvetica Neue", size: "12pt" }), 'font-family: "Inter", "Helvetica Neue", sans-serif; font-size: 12pt');
});

test("nothing from the engine reaches the stylesheet unchecked", () => {
  assert.equal(declarations({ fg: "red; background: url(http://evil)" }), "");
  assert.equal(declarations({ fg: "#12" }), "");
  assert.equal(declarations({ family: 'x"; } body { display: none', size: "1em; x" }), 'font-family: "x  body  display none", sans-serif');
  assert.equal(declarations({ weight: "expression(alert(1))", slant: "}{", line_height: -1, size: 9999 }), "");
});
