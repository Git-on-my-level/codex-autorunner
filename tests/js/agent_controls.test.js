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
  assert.equal(modelSelect.classList.contains("hidden"), false);

  agentSelect.value = "hermes";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(getSelectedAgent(), "hermes");
  assert.equal(modelSelect.classList.contains("hidden"), true);
  assert.equal(modelInput.classList.contains("hidden"), false);
  assert.match(modelInput.placeholder, /Hermes/);
  assert.equal(reasoningSelect.classList.contains("hidden"), true);
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
            default_profile: "m4-pma",
            profiles: [{ id: "m4-pma", display_name: "M4 PMA" }],
          },
          {
            id: "custom-agent",
            name: "Custom Agent",
            capabilities: ["message_turns"],
            default_profile: "fast",
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
  assert.equal(profileSelect.options.length, 0);

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
  assert.equal(profileSelect.options.length, 0);
});

test("shows error state when agents API returns malformed response", async () => {
  __agentControlsTest.reset();
  localStorage.clear();

  globalThis.fetch = async (url) => {
    const href = String(url);
    if (href.endsWith("/hub/pma/agents")) {
      return jsonResponse({ agents: "not-an-array" });
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const modelSelect = document.getElementById("model");

  initAgentControls({ agentSelect, modelSelect });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(__agentControlsTest.agentsLoadFailed, true);
  assert.equal(agentSelect.disabled, true);
  assert.equal(
    agentSelect.options[0].textContent,
    "Failed to load agents \u2014 refresh to retry"
  );
  assert.equal(modelSelect.classList.contains("hidden"), true);
});

test("shows no-agents state when agents API returns empty list", async () => {
  __agentControlsTest.reset();
  localStorage.clear();

  globalThis.fetch = async (url) => {
    const href = String(url);
    if (href.endsWith("/hub/pma/agents")) {
      return jsonResponse({ agents: [], default: "codex" });
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const modelSelect = document.getElementById("model");

  initAgentControls({ agentSelect, modelSelect });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(__agentControlsTest.agentsLoadFailed, false);
  assert.equal(agentSelect.disabled, true);
  assert.equal(
    agentSelect.options[0].textContent,
    "No agents available"
  );
  assert.equal(modelSelect.classList.contains("hidden"), true);
});

test("shows error state when agents API throws", async () => {
  __agentControlsTest.reset();
  localStorage.clear();

  globalThis.fetch = async () => {
    throw new Error("Network error");
  };

  const agentSelect = document.getElementById("agent");
  const modelSelect = document.getElementById("model");

  initAgentControls({ agentSelect, modelSelect });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(__agentControlsTest.agentsLoadFailed, true);
  assert.equal(agentSelect.disabled, true);
  assert.equal(
    agentSelect.options[0].textContent,
    "Failed to load agents \u2014 refresh to retry"
  );
});

test("falls back to first catalog model when catalog has no default", async () => {
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
            capabilities: ["model_listing"],
          },
        ],
        default: "codex",
      });
    }
    if (href.endsWith("/hub/pma/agents/codex/models")) {
      return jsonResponse({
        default_model: "",
        models: [
          { id: "model-a", display_name: "Model A", supports_reasoning: false, reasoning_options: [] },
          { id: "model-b", display_name: "Model B", supports_reasoning: false, reasoning_options: [] },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const modelSelect = document.getElementById("model");

  initAgentControls({ agentSelect, modelSelect });
  await refreshAgentControls({ force: true, reason: "manual" });

  assert.equal(agentSelect.value, "codex");
  assert.equal(modelSelect.disabled, false);
  assert.equal(modelSelect.value, "model-a");
  assert.equal(modelSelect.options.length, 3);
  assert.equal(modelSelect.options[0].textContent, "Select a model\u2026");
  assert.equal(modelSelect.options[0].disabled, true);
  assert.equal(modelSelect.options[1].value, "model-a");
});

test("falls back to first profile when agent has no default_profile", async () => {
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
            capabilities: ["model_listing"],
          },
          {
            id: "hermes",
            name: "Hermes",
            capabilities: ["message_turns"],
            profiles: [
              { id: "alpha", display_name: "Alpha" },
              { id: "beta", display_name: "Beta" },
            ],
          },
        ],
        default: "codex",
      });
    }
    if (href.endsWith("/hub/pma/agents/codex/models")) {
      return jsonResponse({
        default_model: "m1",
        models: [
          { id: "m1", display_name: "M1", supports_reasoning: false, reasoning_options: [] },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${href}`);
  };

  const agentSelect = document.getElementById("agent");
  const profileSelect = document.getElementById("profile");
  const modelSelect = document.getElementById("model");

  initAgentControls({ agentSelect, profileSelect, modelSelect });
  await refreshAgentControls({ force: true, reason: "manual" });

  agentSelect.value = "hermes";
  agentSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await waitForUi();
  await waitForUi();

  assert.equal(profileSelect.classList.contains("hidden"), false);
  assert.equal(profileSelect.disabled, false);
  assert.equal(profileSelect.value, "alpha");
  assert.equal(profileSelect.options[0].textContent, "Select a profile\u2026");
  assert.equal(profileSelect.options[0].disabled, true);
  assert.equal(profileSelect.options[1].value, "alpha");
});
