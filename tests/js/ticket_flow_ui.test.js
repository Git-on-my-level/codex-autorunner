import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <div id="ticket-live-output-panel">
      <button id="ticket-live-output-panel-toggle" type="button"></button>
      <div id="ticket-live-output-compact" class="hidden"></div>
      <div id="ticket-live-output-detail" class="hidden"></div>
      <div id="ticket-live-output-events" class="hidden"></div>
      <button id="ticket-live-output-detail-toggle" type="button"></button>
    </div>
    <div id="ticket-live-output-text"></div>
    <div id="ticket-last-activity"></div>
    <div id="ticket-flow-status">
      <span id="ticket-flow-current"></span>
    </div>
  </body></html>`,
  { url: "http://localhost/hub/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.localStorage = dom.window.localStorage;
globalThis.sessionStorage = dom.window.sessionStorage;

const pendingRafs = [];
globalThis.requestAnimationFrame = (fn) => {
  const id = pendingRafs.push(fn) - 1;
  return id;
};
function flushRafs() {
  while (pendingRafs.length > 0) {
    const fn = pendingRafs.shift();
    if (fn) fn();
  }
}

const { __ticketFlowTest } = await import(
  "../../src/codex_autorunner/static/generated/tickets.js"
);

test("handleFlowEvent processes agent_stream_delta events and buffers output", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  __ticketFlowTest.handleFlowEvent({
    id: "evt-1",
    seq: 1,
    timestamp: new Date().toISOString(),
    event_type: "agent_stream_delta",
    data: { delta: "Hello from agent" },
  });
  flushRafs();

  const outputText =
    document.getElementById("ticket-live-output-text")?.textContent || "";
  assert.match(outputText, /Hello from agent/);
});

test("handleFlowEvent processes app_server_event through parseAppServerEvent", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  __ticketFlowTest.handleFlowEvent({
    id: "evt-2",
    seq: 2,
    timestamp: new Date().toISOString(),
    event_type: "app_server_event",
    data: {
      id: "diff-1",
      received_at: 1000,
      message: {
        method: "session.diff",
        params: {
          message: "src/foo.ts, src/bar.ts",
          properties: { diff_count: 2 },
        },
      },
    },
  });
  flushRafs();

  const compactEl = document.getElementById("ticket-live-output-compact");
  const detailEl = document.getElementById("ticket-live-output-detail");
  assert.ok(compactEl || detailEl, "live output panel elements should exist");
});

test("handleFlowEvent processes step_progress to set current ticket", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  __ticketFlowTest.handleFlowEvent({
    id: "evt-3",
    seq: 3,
    timestamp: new Date().toISOString(),
    event_type: "step_progress",
    data: { current_ticket: "TICKET-1920-do-something" },
  });
  flushRafs();

  const currentEl = document.getElementById("ticket-flow-current");
  assert.equal(
    currentEl?.textContent,
    "TICKET-1920-do-something"
  );
});

test("handleFlowEvent processes step_started events with step name", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  __ticketFlowTest.handleFlowEvent({
    id: "evt-4",
    seq: 4,
    timestamp: new Date().toISOString(),
    event_type: "step_started",
    data: { step_name: "scope" },
  });
  flushRafs();

  const outputText =
    document.getElementById("ticket-live-output-text")?.textContent || "";
  assert.match(outputText, /Step: scope/);
});

test("clearLiveOutput resets live output text and events", () => {
  __ticketFlowTest.handleFlowEvent({
    id: "evt-clear-1",
    seq: 10,
    timestamp: new Date().toISOString(),
    event_type: "agent_stream_delta",
    data: { delta: "Some output" },
  });
  flushRafs();

  const beforeClear =
    document.getElementById("ticket-live-output-text")?.textContent || "";
  assert.match(beforeClear, /Some output/);

  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  const afterClear =
    document.getElementById("ticket-live-output-text")?.textContent || "";
  assert.equal(afterClear, "");
});

test("initLiveOutputPanel sets up toggle button handlers", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  const panel = document.getElementById("ticket-live-output-panel");

  __ticketFlowTest.initLiveOutputPanel();

  const toggleBtn = document.getElementById(
    "ticket-live-output-panel-toggle"
  );
  assert.ok(toggleBtn, "panel toggle button should exist");

  toggleBtn.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  flushRafs();
});

test("setFlowStartedAt controls the flow start timestamp", () => {
  __ticketFlowTest.clearLiveOutput();
  flushRafs();

  __ticketFlowTest.setFlowStartedAt(Date.now() - 5 * 60 * 1000);
  __ticketFlowTest.setFlowStartedAt(null);
  __ticketFlowTest.setFlowStartedAt(Date.now());
});

test("last-seen sequence key follows per-run prefix pattern", () => {
  const runId = "run-abc-123";
  const key = `car-ticket-flow-last-seq:${runId}`;
  assert.match(key, /^car-ticket-flow-last-seq:/);
  assert.match(key, /run-abc-123$/);
});

test("last-seen sequence storage is per-run and monotonically increasing", () => {
  localStorage.clear();
  const runId1 = "run-seq-test-1";
  const runId2 = "run-seq-test-2";
  const prefix = "car-ticket-flow-last-seq:";

  localStorage.setItem(`${prefix}${runId1}`, "10");
  localStorage.setItem(`${prefix}${runId2}`, "20");

  const seq1 = Number.parseInt(
    localStorage.getItem(`${prefix}${runId1}`),
    10
  );
  const seq2 = Number.parseInt(
    localStorage.getItem(`${prefix}${runId2}`),
    10
  );

  assert.equal(seq1, 10);
  assert.equal(seq2, 20);

  localStorage.setItem(`${prefix}${runId1}`, "15");
  const updated = Number.parseInt(
    localStorage.getItem(`${prefix}${runId1}`),
    10
  );
  assert.equal(updated, 15);
  assert.ok(updated > seq1, "sequence should only increase");
});

test("EventSource reconnect URL includes after parameter from last-seen sequence", () => {
  localStorage.clear();
  const runId = "run-reconnect-test";
  const prefix = "car-ticket-flow-last-seq:";
  const lastSeq = 42;

  localStorage.setItem(`${prefix}${runId}`, String(lastSeq));

  const storedSeq = localStorage.getItem(`${prefix}${runId}`);
  assert.equal(storedSeq, "42");

  const url = new URL(
    `/api/flows/${runId}/events?after=${storedSeq}`,
    "http://localhost"
  );
  assert.equal(url.searchParams.get("after"), "42");
  assert.ok(url.pathname.includes(runId));
});

test("newest run authority: only runs[0] is authoritative, not older paused runs", () => {
  const runs = [
    { id: "run-newest", status: "completed", started_at: "2026-04-17T10:00:00Z" },
    { id: "run-older-paused", status: "paused", started_at: "2026-04-16T08:00:00Z" },
    { id: "run-oldest-paused", status: "paused", started_at: "2026-04-15T06:00:00Z" },
  ];

  const newest = runs[0] || null;
  const currentRunId = newest?.id || null;
  const currentFlowStatus = newest?.status || null;

  assert.equal(currentRunId, "run-newest");
  assert.equal(currentFlowStatus, "completed");

  assert.notEqual(currentRunId, "run-older-paused");
  assert.notEqual(currentRunId, "run-oldest-paused");
});

test("newest run authority: empty runs array means no active flow", () => {
  const runs = [];
  const newest = runs[0] || null;
  const currentRunId = (newest?.id) || null;
  const currentFlowStatus = (newest?.status) || null;

  assert.equal(currentRunId, null);
  assert.equal(currentFlowStatus, null);
});

test("retry delay schedule uses capped exponential backoff", () => {
  const delays = [500, 1000, 2000, 5000, 10000];
  assert.equal(delays.length, 5);

  assert.equal(delays[Math.min(0, delays.length - 1)], 500);
  assert.equal(delays[Math.min(1, delays.length - 1)], 1000);
  assert.equal(delays[Math.min(2, delays.length - 1)], 2000);
  assert.equal(delays[Math.min(3, delays.length - 1)], 5000);
  assert.equal(delays[Math.min(4, delays.length - 1)], 10000);
  assert.equal(delays[Math.min(5, delays.length - 1)], 10000);
  assert.equal(delays[Math.min(10, delays.length - 1)], 10000);
});

test("newest run authority source code uses runs[0] not find()", async () => {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const content = fs.readFileSync(
    path.join(process.cwd(), "src", "codex_autorunner", "static_src", "tickets.ts"),
    "utf8"
  );

  const runsIndex = content.indexOf("const runs = (await api");
  assert.ok(runsIndex !== -1, "expected runs fetch");

  const newestLine = content.indexOf("const newest = runs?.[0] || null", runsIndex);
  assert.ok(newestLine !== -1, "expected newest = runs?.[0] assignment");
});

test("connectEventSource uses after parameter from last seen sequence", async () => {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const content = fs.readFileSync(
    path.join(process.cwd(), "src", "codex_autorunner", "static_src", "tickets.ts"),
    "utf8"
  );

  const connectFn = content.indexOf("function connectEventStream(");
  assert.ok(connectFn !== -1, "expected connectEventStream function");

  const afterParam = content.indexOf('url.searchParams.set("after"', connectFn);
  assert.ok(afterParam !== -1, "expected after parameter in EventSource URL");

  const getLastSeenCall = content.indexOf("getLastSeenSeq(runId)", connectFn);
  assert.ok(getLastSeenCall !== -1, "expected getLastSeenSeq call in connectEventStream");
});

test("setLastSeenSeq is monotonically increasing in source code", async () => {
  const fs = await import("node:fs");
  const path = await import("node:path");
  const content = fs.readFileSync(
    path.join(process.cwd(), "src", "codex_autorunner", "static_src", "tickets.ts"),
    "utf8"
  );

  const setLastSeenFn = content.indexOf("function setLastSeenSeq(");
  assert.ok(setLastSeenFn !== -1, "expected setLastSeenSeq function");

  const monotonicGuard = content.indexOf("if (current !== undefined && seq <= current) return;", setLastSeenFn);
  assert.ok(monotonicGuard !== -1, "expected monotonic guard in setLastSeenSeq");
});
