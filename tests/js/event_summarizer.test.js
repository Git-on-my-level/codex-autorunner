import assert from "node:assert/strict";
import { test } from "node:test";

const { summarizeEvents, renderCompactSummary } = await import(
  "../../src/codex_autorunner/static/generated/eventSummarizer.js"
);

function event(overrides = {}) {
  return {
    id: "evt-1",
    title: "Event",
    summary: "",
    detail: "",
    kind: "event",
    isSignificant: true,
    time: 0,
    method: "status",
    ...overrides,
  };
}

test("keeps full output text in compact summary", () => {
  const outputText = "output " + "x".repeat(180);
  const summary = summarizeEvents(
    [
      event({
        id: "evt-output",
        title: "Output",
        kind: "output",
        summary: outputText,
        method: "item/outputDelta",
      }),
    ],
    { maxTextLength: 20, maxActions: 5, now: 1000, startTime: 0 }
  );

  const rendered = renderCompactSummary(summary);
  assert.match(rendered, /↻ output:/);
  assert.match(rendered, /x{120}/);
  assert.doesNotMatch(rendered, /…/);
});

test("still trims thinking and tool text and keeps latest thinking entry", () => {
  const summary = summarizeEvents(
    [
      event({
        id: "evt-tool",
        title: "Tool",
        kind: "tool",
        summary: "tool " + "a".repeat(80),
        method: "item/completed",
      }),
      event({
        id: "evt-thinking-1",
        title: "Thinking",
        kind: "thinking",
        summary: "thinking " + "b".repeat(80),
        method: "item/reasoning/summaryTextDelta",
      }),
      event({
        id: "evt-thinking-2",
        title: "Thinking",
        kind: "thinking",
        summary: "latest " + "c".repeat(80),
        method: "item/reasoning/summaryTextDelta",
      }),
    ],
    { maxTextLength: 20, maxActions: 5, now: 1000, startTime: 0 }
  );

  const rendered = renderCompactSummary(summary);
  const thinkingActions = summary.actions.filter((action) => action.label === "thinking");

  assert.equal(thinkingActions.length, 1);
  assert.match(rendered, /✓ tool: .*…/);
  assert.match(rendered, /🧠 .*…/);
  assert.match(rendered, /latest/);
});
