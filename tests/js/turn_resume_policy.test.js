import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body></body></html>`,
  { url: "http://localhost/hub/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.localStorage = dom.window.localStorage;

const {
  createTurnRecoveryTracker,
  cancelActiveTurnSync,
  cancelActiveTurnAndWait,
  pendingTurnMatches,
  scheduleRecoveryRetry,
  ACTIVE_TURN_RECOVERY_STALE_MESSAGE,
  DEFAULT_RECOVERY_MAX_ATTEMPTS,
} = await import(
  "../../src/codex_autorunner/static/generated/sharedTurnLifecycle.js"
).then((m) => m.__turnRecoveryPolicyTest);

test("tracker starts in recovering phase with zero attempts", () => {
  const tracker = createTurnRecoveryTracker();
  assert.equal(tracker.phase, "recovering");
  assert.equal(tracker.attempts, 0);
  assert.equal(tracker.maxAttempts, DEFAULT_RECOVERY_MAX_ATTEMPTS);
});

test("tracker tick increments attempts and returns true while under max", () => {
  const tracker = createTurnRecoveryTracker(5);
  assert.equal(tracker.tick(), true);
  assert.equal(tracker.attempts, 1);
  assert.equal(tracker.phase, "recovering");
  assert.equal(tracker.tick(), true);
  assert.equal(tracker.attempts, 2);
  assert.equal(tracker.phase, "recovering");
});

test("tracker transitions to stale when max attempts exceeded", () => {
  const tracker = createTurnRecoveryTracker(3);
  assert.equal(tracker.tick(), true);
  assert.equal(tracker.tick(), true);
  assert.equal(tracker.tick(), false);
  assert.equal(tracker.phase, "stale");
  assert.equal(tracker.attempts, 3);
});

test("tracker tick returns false after going stale", () => {
  const tracker = createTurnRecoveryTracker(1);
  assert.equal(tracker.tick(), false);
  assert.equal(tracker.phase, "stale");
  assert.equal(tracker.tick(), false);
  assert.equal(tracker.attempts, 1);
});

test("tracker respects custom max attempts", () => {
  const tracker = createTurnRecoveryTracker(10);
  assert.equal(tracker.maxAttempts, 10);
  for (let i = 0; i < 9; i++) {
    assert.equal(tracker.tick(), true, `tick ${i + 1} should succeed`);
  }
  assert.equal(tracker.tick(), false, "tick 10 should fail");
  assert.equal(tracker.phase, "stale");
});

test("cancelActiveTurnSync calls abortController and turnEventsCtrl.abort", () => {
  let controllerAborted = false;
  let turnEventsAborted = false;
  let pendingCleared = false;
  let interruptCalled = false;

  cancelActiveTurnSync({
    abortController() {
      controllerAborted = true;
    },
    turnEventsCtrl: {
      abort() {
        turnEventsAborted = true;
      },
    },
    interruptServer() {
      interruptCalled = true;
      return Promise.resolve();
    },
    clearPending() {
      pendingCleared = true;
    },
  });

  assert.equal(controllerAborted, true);
  assert.equal(turnEventsAborted, true);
  assert.equal(pendingCleared, true);
  assert.equal(interruptCalled, true);
});

test("cancelActiveTurnSync works without optional callbacks", () => {
  let controllerAborted = false;
  let turnEventsAborted = false;

  cancelActiveTurnSync({
    abortController() {
      controllerAborted = true;
    },
    turnEventsCtrl: {
      abort() {
        turnEventsAborted = true;
      },
    },
  });

  assert.equal(controllerAborted, true);
  assert.equal(turnEventsAborted, true);
});

test("cancelActiveTurnSync swallows interrupt rejections", async () => {
  let controllerAborted = false;

  cancelActiveTurnSync({
    abortController() {
      controllerAborted = true;
    },
    turnEventsCtrl: { abort() {} },
    interruptServer() {
      return Promise.reject(new Error("network"));
    },
  });

  await new Promise((r) => setTimeout(r, 10));
  assert.equal(controllerAborted, true);
});

test("cancelActiveTurnAndWait resolves after interrupt completion", async () => {
  const events = [];
  let releaseInterrupt;
  const interruptDone = new Promise((resolve) => {
    releaseInterrupt = resolve;
  });

  const cancelPromise = cancelActiveTurnAndWait({
    abortController() {
      events.push("abort-controller");
    },
    turnEventsCtrl: {
      abort() {
        events.push("abort-events");
      },
    },
    clearPending() {
      events.push("clear-pending");
    },
    interruptServer() {
      events.push("interrupt-start");
      return interruptDone.then(() => {
        events.push("interrupt-finish");
      });
    },
  });

  events.push("after-call");
  await Promise.resolve();
  assert.deepEqual(events, [
    "abort-controller",
    "abort-events",
    "clear-pending",
    "interrupt-start",
    "after-call",
  ]);

  releaseInterrupt();
  await cancelPromise;
  assert.deepEqual(events, [
    "abort-controller",
    "abort-events",
    "clear-pending",
    "interrupt-start",
    "after-call",
    "interrupt-finish",
  ]);
});

test("pendingTurnMatches requires same turn identity and target", () => {
  const expected = {
    clientTurnId: "turn-1",
    message: "hello",
    startedAtMs: 123,
    target: "contextspace:active_context",
  };
  assert.equal(pendingTurnMatches(expected, { ...expected }), true);
  assert.equal(
    pendingTurnMatches(expected, { ...expected, clientTurnId: "turn-2" }),
    false
  );
  assert.equal(
    pendingTurnMatches(expected, { ...expected, startedAtMs: 124 }),
    false
  );
  assert.equal(
    pendingTurnMatches(expected, { ...expected, target: "contextspace:spec" }),
    false
  );
  assert.equal(pendingTurnMatches(expected, null), false);
});

test("ACTIVE_TURN_RECOVERY_STALE_MESSAGE is a non-empty guidance string", () => {
  assert.ok(typeof ACTIVE_TURN_RECOVERY_STALE_MESSAGE === "string");
  assert.ok(ACTIVE_TURN_RECOVERY_STALE_MESSAGE.length > 0);
  assert.ok(
    ACTIVE_TURN_RECOVERY_STALE_MESSAGE.toLowerCase().includes("retry") ||
    ACTIVE_TURN_RECOVERY_STALE_MESSAGE.toLowerCase().includes("new thread")
  );
});

test("scheduleRecoveryRetry schedules a timeout and calls retryFn", async () => {
  const tracker = createTurnRecoveryTracker(5);
  let retryCount = 0;

  scheduleRecoveryRetry({
    tracker,
    retryFn: async () => {
      retryCount += 1;
    },
    intervalMs: 10,
  });

  assert.equal(retryCount, 0);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(retryCount, 1);
  assert.equal(tracker.attempts, 1);
});

test("scheduleRecoveryRetry calls onStale when tracker is exhausted", async () => {
  const tracker = createTurnRecoveryTracker(1);
  let staleCalled = false;

  scheduleRecoveryRetry({
    tracker,
    retryFn: async () => {},
    onStale: () => {
      staleCalled = true;
    },
    intervalMs: 10,
  });

  await new Promise((r) => setTimeout(r, 50));
  assert.equal(staleCalled, true);
  assert.equal(tracker.phase, "stale");
});

test("scheduleRecoveryRetry does nothing when tracker is already stale", async () => {
  const tracker = createTurnRecoveryTracker(1);
  tracker.tick();

  let retryCount = 0;
  scheduleRecoveryRetry({
    tracker,
    retryFn: async () => {
      retryCount += 1;
    },
    intervalMs: 10,
  });

  await new Promise((r) => setTimeout(r, 50));
  assert.equal(retryCount, 0);
});

test("DEFAULT_RECOVERY_MAX_ATTEMPTS is 30", () => {
  assert.equal(DEFAULT_RECOVERY_MAX_ATTEMPTS, 30);
});
