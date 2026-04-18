import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const ROOT = process.cwd();

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.CustomEvent = dom.window.CustomEvent;

const { createSmartRefresh } = await import(
  "../../src/codex_autorunner/static/generated/smartRefresh.js"
);

const { subscribe, publish } = await import(
  "../../src/codex_autorunner/static/generated/bus.js"
);

test("smartRefresh does not call render when signature is unchanged", async () => {
  let renderCount = 0;
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => { renderCount++; },
  });

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 1);

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 1, "should not re-render for same signature");
});

test("smartRefresh calls render when signature changes", async () => {
  let renderCount = 0;
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => { renderCount++; },
  });

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 1);

  await refresher.refresh(() => Promise.resolve({ version: "v2" }));
  assert.equal(renderCount, 2, "should re-render for changed signature");
});

test("smartRefresh calls render on forced refresh even with same signature", async () => {
  let renderCount = 0;
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => { renderCount++; },
  });

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 1);

  await refresher.refresh(
    () => Promise.resolve({ version: "v1" }),
    { force: true }
  );
  assert.equal(renderCount, 2, "should re-render when forced");
});

test("smartRefresh calls onSkip when signature is unchanged", async () => {
  let skipCalled = false;
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => {},
    onSkip: () => { skipCalled = true; },
  });

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(skipCalled, false);

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(skipCalled, true, "should call onSkip for unchanged data");
});

test("smartRefresh reports updated=false on skipped refresh", async () => {
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => {},
  });

  const first = await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(first.updated, true);

  const second = await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(second.updated, false);
  assert.equal(second.signature, "v1");
});

test("smartRefresh reset clears signature for next initial render", async () => {
  let renderCount = 0;
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: () => { renderCount++; },
  });

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 1);

  refresher.reset();
  assert.equal(refresher.getSignature(), null);

  await refresher.refresh(() => Promise.resolve({ version: "v1" }));
  assert.equal(renderCount, 2, "should re-render after reset even with same data");
});

test("smartRefresh context reports correct reason", async () => {
  const reasons = [];
  const refresher = createSmartRefresh({
    getSignature: (payload) => payload.version,
    render: (_payload, ctx) => { reasons.push(ctx.reason); },
  });

  await refresher.refresh(
    () => Promise.resolve({ version: "v1" }),
    { reason: "initial" }
  );
  assert.equal(reasons[0], "initial");

  await refresher.refresh(
    () => Promise.resolve({ version: "v2" }),
    { reason: "manual" }
  );
  assert.equal(reasons[1], "manual");
});

test("bus publish delivers events to subscribers", () => {
  const received = [];
  const unsub = subscribe("test:bus:evt", (payload) => {
    received.push(payload);
  });

  publish("test:bus:evt", { value: 1 });
  publish("test:bus:evt", { value: 2 });

  assert.equal(received.length, 2);
  assert.deepEqual(received[0], { value: 1 });
  assert.deepEqual(received[1], { value: 2 });

  unsub();
});

test("bus unsubscribe stops delivery", () => {
  let count = 0;
  const unsub = subscribe("test:bus:unsub", () => { count++; });

  publish("test:bus:unsub", {});
  assert.equal(count, 1);

  unsub();
  publish("test:bus:unsub", {});
  assert.equal(count, 1, "should not receive after unsubscribe");
});

test("bus delivers to multiple independent subscribers", () => {
  const a = [];
  const b = [];
  const unsubA = subscribe("test:bus:multi", (p) => { a.push(p); });
  const unsubB = subscribe("test:bus:multi", (p) => { b.push(p); });

  publish("test:bus:multi", { x: 1 });
  assert.equal(a.length, 1);
  assert.equal(b.length, 1);

  unsubA();
  publish("test:bus:multi", { x: 2 });
  assert.equal(a.length, 1);
  assert.equal(b.length, 2);

  unsubB();
});

test("liveUpdates compares run identity for invalidation", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /last_run_id/, "should compare last_run_id");
  assert.match(content, /last_run_finished_at/, "should compare last_run_finished_at");
  assert.match(content, /runs:invalidate/, "should publish runs:invalidate");
});

test("liveUpdates compares todo counts for invalidation", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /outstanding_count/, "should compare outstanding_count");
  assert.match(content, /done_count/, "should compare done_count");
  assert.match(content, /todo:invalidate/, "should publish todo:invalidate");
});

test("liveUpdates compares runner status for invalidation", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /runner_pid/, "should compare runner_pid");
  assert.match(content, /runner:status/, "should publish runner:status");
});

test("liveUpdates debounces invalidation flush", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /INVALIDATION_DEBOUNCE_MS/, "should define debounce constant");
  assert.match(content, /setTimeout.*flushInvalidations/, "should debounce with setTimeout");
  assert.match(content, /pendingInvalidations/, "should collect pending invalidations");
});

test("liveUpdates subscribes to state:update on init", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /subscribe\("state:update"/, "should subscribe to state:update");
  assert.match(content, /initialized/, "should guard against double init");
});

test("liveUpdates uses bus.publish for invalidation events", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  assert.match(content, /from ["']\.\/bus\.js["']/, "should import from bus.js");
  assert.match(content, /publish\(key/, "should use bus publish for invalidation keys");
});

test("liveUpdates normalizes state with null defaults", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "liveUpdates.ts"),
    "utf8"
  );

  const normalizeIndex = content.indexOf("function normalizeState");
  assert.ok(normalizeIndex !== -1, "should have normalizeState function");
  assert.match(content, /\?\?\s*null/, "should default to null");
});
