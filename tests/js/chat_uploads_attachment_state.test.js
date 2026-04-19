import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <textarea id="test-chat-input"></textarea>
  </body></html>`,
  { url: "http://localhost/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;

const {
  createAttachmentTracker,
} = await import(
  "../../src/codex_autorunner/static/generated/chatUploads.js"
);

test("attachment tracker starts empty", () => {
  const tracker = createAttachmentTracker();
  assert.deepEqual(tracker.getAttachments(), []);
  assert.equal(tracker.getPendingCount(), 0);
  assert.equal(tracker.getSummaryText(), "");
});

test("attachment tracker tracks uploading entries", () => {
  const tracker = createAttachmentTracker();
  tracker._add({ name: "screenshot.png", url: "", status: "uploading" });
  tracker._add({ name: "diagram.png", url: "", status: "uploading" });
  assert.equal(tracker.getAttachments().length, 2);
  assert.equal(tracker.getPendingCount(), 2);
  assert.ok(tracker.getSummaryText().includes("2 uploading"));
});

test("attachment tracker transitions from uploading to uploaded", () => {
  const tracker = createAttachmentTracker();
  tracker._add({ name: "screenshot.png", url: "", status: "uploading" });
  tracker._update("screenshot.png", "uploaded");
  assert.equal(tracker.getPendingCount(), 0);
  assert.equal(tracker.getAttachments()[0].status, "uploaded");
  const summary = tracker.getSummaryText();
  assert.ok(summary.includes("1 uploaded"));
  assert.ok(summary.includes("screenshot.png"));
});

test("attachment tracker tracks failed uploads", () => {
  const tracker = createAttachmentTracker();
  tracker._add({ name: "bad.png", url: "", status: "uploading" });
  tracker._update("bad.png", "failed", "Network error");
  assert.equal(tracker.getPendingCount(), 0);
  assert.equal(tracker.getAttachments()[0].status, "failed");
  assert.equal(tracker.getAttachments()[0].error, "Network error");
  assert.ok(tracker.getSummaryText().includes("1 failed"));
});

test("attachment tracker summary combines multiple statuses", () => {
  const tracker = createAttachmentTracker();
  tracker._add({ name: "a.png", url: "/a.png", status: "uploaded" });
  tracker._add({ name: "b.png", url: "", status: "uploading" });
  tracker._add({ name: "c.png", url: "", status: "failed", error: "timeout" });
  const summary = tracker.getSummaryText();
  assert.ok(summary.includes("1 uploaded"));
  assert.ok(summary.includes("1 uploading"));
  assert.ok(summary.includes("1 failed"));
});

test("attachment tracker getAttachments returns a shallow copy of the array", () => {
  const tracker = createAttachmentTracker();
  tracker._add({ name: "a.png", url: "", status: "uploading" });
  const attachments = tracker.getAttachments();
  attachments.push({ name: "b.png", url: "", status: "uploading" });
  assert.equal(tracker.getAttachments().length, 1);
});
