import assert from "node:assert/strict";
import { test } from "node:test";

const {
  getUiMockScenarioList,
  getUiMockScenarioOrDefault,
} = await import(
  "../../src/codex_autorunner/static/generated/uiMockScenarios.js"
);

test("ui mock registry lists every known scenario with matching ids", () => {
  const list = getUiMockScenarioList();
  assert.ok(list.length > 0);
  const seen = new Set();
  for (const row of list) {
    const { scenario, resolvedId, fallback } = getUiMockScenarioOrDefault(row.id);
    assert.equal(fallback, false);
    assert.equal(resolvedId, row.id);
    assert.equal(scenario.id, row.id);
    assert.equal(scenario.label, row.label);
    assert.ok(!seen.has(row.id));
    seen.add(row.id);
  }
});

test("getUiMockScenarioOrDefault recovers to empty for unknown id", () => {
  const { scenario, resolvedId, fallback } = getUiMockScenarioOrDefault("no_such_thing_123");
  assert.equal(fallback, true);
  assert.equal(resolvedId, "empty");
  assert.equal(scenario.id, "empty");
});
