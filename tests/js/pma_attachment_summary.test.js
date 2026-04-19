import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!doctype html><html><body></body></html>`, {
  url: "http://localhost/",
});

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

const { __pmaTest } = await import(
  "../../src/codex_autorunner/static/generated/pma.js"
);

test("outbox attachment summary uses pre-finalization baseline snapshot", () => {
  const listing = {
    inbox: [],
    outbox: [
      { name: "existing.txt", url: "/hub/pma/files/outbox/existing.txt" },
      { name: "report.md", url: "/hub/pma/files/outbox/report.md" },
      { name: "chart.png", url: "/hub/pma/files/outbox/chart.png" },
    ],
    consumed: [],
    dismissed: [],
  };
  const baseline = new Set(["existing.txt"]);

  const summary = __pmaTest.buildOutboxAttachmentSummary(listing, baseline);

  assert.match(summary, /\*\*Outbox files \(download\):\*\*/);
  assert.match(summary, /\[report\.md\]\(http:\/\/localhost\/hub\/pma\/files\/outbox\/report\.md\)/);
  assert.match(summary, /\[chart\.png\]\(http:\/\/localhost\/hub\/pma\/files\/outbox\/chart\.png\)/);
  assert.doesNotMatch(summary, /existing\.txt/);
});

test("outbox attachment summary is empty when there are no new files", () => {
  const listing = {
    inbox: [],
    outbox: [{ name: "existing.txt", url: "/hub/pma/files/outbox/existing.txt" }],
    consumed: [],
    dismissed: [],
  };
  const baseline = new Set(["existing.txt"]);

  const summary = __pmaTest.buildOutboxAttachmentSummary(listing, baseline);

  assert.equal(summary, "");
});
