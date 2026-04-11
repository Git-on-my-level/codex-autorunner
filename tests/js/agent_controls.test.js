import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <select id="agent"></select>
    <select id="profile"></select>
    <select id="model"></select>
    <input id="model-input" class="hidden" type="text" />
    <select id="reasoning"></select>
  </body></html>`,
  { url: "http://localhost/hub/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
globalThis.Event = dom.window.Event;
globalThis.localStorage = dom.window.localStorage;

const {
  __agentControlsTest,
  getSelectedAgent,
  getSelectedModel,
  initAgentControls,
  refreshAgentControls,
} = await import("../../src/codex_autorunner/static/generated/agentControls.js");

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function waitForUi() {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

test("Hermes uses manual model override mode without fetching a catalog", async () => {
  __agentControlsTest.reset();
  localStorage.clear();

  const calls = [];
  globalThis.fetch = async (url) => {
    const href = String(url);
    calls.push(href);
    if (href.endsWith("/hub/pma/agents")) {
      return jsonResponse({
        agents: [
          {
            id: "codex",
            name: "Codex",
            capabilities: ["model_listing", "message_turns"],
          },
          {
            id: "hermes",
            name: "Hermes",
            capabilities: ["message_turns"],
          },
        ],
        default: "codex",
      });
    }
    if (href.endsWith("/hub/pma/agents/codex/models")) {
      return jsonResponse({
        default_model: "gpt-5.4",
        models: [
          {
            id: "gpt-5.4",
            display_name: "GPT-5.4",
            supports_reasoning: true,
            reasoning_options: ["medium", "high"],
          },
        ],
      });
    }
    if (href.endsWith("/hub/pma/agents/hermes/models")) {
      return jsonResponse(
        { detail: "Agent 'hermes' does not support capability 'model_listing'" },
        400
      );
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const modelSelect = document.getElementById("model");
  const modelInput = document.getElementById("model-input");
  const reasoningSelect = document.getElementById("reasoning");

  initAgentControls({
    agentSelect,
    modelSelect,
    modelInput,
    reasoningSelect,
  });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(getSelectedAgent(), "codex");
  assert.equal(modelSelect.disabled, false);

  agentSelect.value = "hermes";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(getSelectedAgent(), "hermes");
  assert.equal(modelSelect.disabled, true);
  assert.equal(modelInput.classList.contains("hidden"), false);
  assert.match(modelInput.placeholder, /Hermes/);
  assert.equal(reasoningSelect.disabled, true);
  assert.equal(
    calls.some((href) => href.endsWith("/hub/pma/agents/hermes/models")),
    false
  );

  modelInput.value = "hermes/free-form-model";
  modelInput.dispatchEvent(new Event("input", { bubbles: true }));

  assert.equal(getSelectedModel("hermes"), "hermes/free-form-model");
});

test("profile picker only shows for agents that expose profiles", async () => {
  __agentControlsTest.reset();
  localStorage.clear();

  globalThis.fetch = async (url) => {
    const href = String(url);
    if (href.endsWith("/hub/pma/agents")) {
      return jsonResponse({
        agents: [
          {
            id: "codex",
            name: "Codex",
            capabilities: ["model_listing", "message_turns"],
          },
          {
            id: "hermes",
            name: "Hermes",
            capabilities: ["message_turns"],
            profiles: [{ id: "m4-pma", display_name: "M4 PMA" }],
          },
          {
            id: "custom-agent",
            name: "Custom Agent",
            capabilities: ["message_turns"],
            profiles: [{ id: "fast", display_name: "Fast" }],
          },
        ],
        default: "codex",
      });
    }
    if (href.endsWith("/hub/pma/agents/codex/models")) {
      return jsonResponse({
        default_model: "gpt-5.4",
        models: [
          {
            id: "gpt-5.4",
            display_name: "GPT-5.4",
            supports_reasoning: true,
            reasoning_options: ["medium", "high"],
          },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const profileSelect = document.getElementById("profile");
  const modelSelect = document.getElementById("model");
  const modelInput = document.getElementById("model-input");
  const reasoningSelect = document.getElementById("reasoning");

  initAgentControls({
    agentSelect,
    profileSelect,
    modelSelect,
    modelInput,
    reasoningSelect,
  });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(profileSelect.classList.contains("hidden"), true);
  assert.equal(profileSelect.disabled, true);

  agentSelect.value = "hermes";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(profileSelect.classList.contains("hidden"), false);
  assert.equal(profileSelect.disabled, false);
  assert.equal(profileSelect.value, "m4-pma");

  agentSelect.value = "custom-agent";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(profileSelect.classList.contains("hidden"), false);
  assert.equal(profileSelect.disabled, false);
  assert.equal(profileSelect.value, "fast");

  agentSelect.value = "codex";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(profileSelect.classList.contains("hidden"), true);
  assert.equal(profileSelect.disabled, true);
});
