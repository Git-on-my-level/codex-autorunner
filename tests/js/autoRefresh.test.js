import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "dangerously" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;

const { registerAutoRefresh, setAutoRefreshEnabled, getAutoRefreshPauseReason, triggerRefresh } =
  await import("../../src/codex_autorunner/static/generated/autoRefresh.js");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test("registerAutoRefresh returns cleanup function", () => {
  const cleanup = registerAutoRefresh("test:cleanup:1", {
    callback: async () => {},
    interval: 1000,
  });
  assert.ok(typeof cleanup === "function");
  cleanup();
});

test("setAutoRefreshEnabled(false) pauses and tracks reason", () => {
  setAutoRefreshEnabled(false, "testing pause");
  assert.strictEqual(getAutoRefreshPauseReason(), "testing pause");

  setAutoRefreshEnabled(true);
  assert.strictEqual(getAutoRefreshPauseReason(), null);
});

test("re-registering same id replaces previous", () => {
  registerAutoRefresh("test:replace:1", {
    callback: async () => {},
    interval: 1000,
  });

  registerAutoRefresh("test:replace:1", {
    callback: async () => {},
    interval: 2000,
  });

  const cleanup = registerAutoRefresh("test:replace:1", {
    callback: async () => {},
    interval: 1000,
  });
  cleanup();

  assert.ok(true, "re-registering same id should not throw");
});

test("immediate option triggers callback on registration", async () => {
  let called = false;
  const cleanup = registerAutoRefresh("test:immediate", {
    callback: async () => { called = true; },
    interval: 1000,
    immediate: true,
  });

  await sleep(20);
  assert.ok(called, "immediate option should trigger callback");
  cleanup();
});

test("cleanup is idempotent", () => {
  const cleanup = registerAutoRefresh("test:idempotent", {
    callback: async () => {},
    interval: 1000,
  });
  cleanup();
  cleanup();
  assert.ok(true, "multiple cleanups should not throw");
});

test("triggerRefresh invokes callback with manual reason", async () => {
  const reasons = [];
  const cleanup = registerAutoRefresh("test:trigger", {
    callback: async (ctx) => { reasons.push(ctx.reason); },
    interval: 1000,
  });

  triggerRefresh("test:trigger");
  await sleep(20);

  assert.ok(reasons.includes("manual"), "triggerRefresh should invoke callback with manual reason");
  cleanup();
});

test("setAutoRefreshEnabled(false) then true restores refreshers", async () => {
  let callCount = 0;
  const cleanup = registerAutoRefresh("test:pause:restore", {
    callback: async () => { callCount++; },
    interval: 1000,
    immediate: true,
  });

  await sleep(20);
  const beforePause = callCount;

  setAutoRefreshEnabled(false, "testing");
  assert.strictEqual(getAutoRefreshPauseReason(), "testing");

  setAutoRefreshEnabled(true);
  assert.strictEqual(getAutoRefreshPauseReason(), null);

  triggerRefresh("test:pause:restore");
  await sleep(20);

  assert.ok(callCount > beforePause, "should be able to trigger after re-enabling");
  cleanup();
});

test("multiple independent refreshers can coexist", () => {
  const cleanup1 = registerAutoRefresh("test:multi:independent:1", {
    callback: async () => {},
    interval: 1000,
  });
  const cleanup2 = registerAutoRefresh("test:multi:independent:2", {
    callback: async () => {},
    interval: 2000,
  });

  cleanup1();
  cleanup2();

  assert.ok(true, "multiple independent refreshers should work");
});

test("re-registering the same id clears the previous timer", async () => {
  let oldTimerCalls = 0;
  registerAutoRefresh("test:replace:timer", {
    callback: async () => {
      oldTimerCalls += 1;
    },
    interval: 5,
  });

  await sleep(20);
  assert.ok(oldTimerCalls > 0, "expected the original timer to run before replacement");

  const cleanup = registerAutoRefresh("test:replace:timer", {
    callback: async () => {},
    interval: 100,
  });
  const countAtReplacement = oldTimerCalls;

  await sleep(30);
  cleanup();

  assert.strictEqual(
    oldTimerCalls,
    countAtReplacement,
    "re-registering should stop the previous interval immediately"
  );
});

test("stale cleanup does not unregister the replacement refresher", async () => {
  const observed = [];
  const staleCleanup = registerAutoRefresh("test:stale:cleanup", {
    callback: async () => {
      observed.push("old");
    },
    interval: 1000,
  });
  const cleanup = registerAutoRefresh("test:stale:cleanup", {
    callback: async () => {
      observed.push("new");
    },
    interval: 1000,
  });

  staleCleanup();
  triggerRefresh("test:stale:cleanup");
  await sleep(20);
  cleanup();

  assert.deepStrictEqual(observed, ["new"]);
});
