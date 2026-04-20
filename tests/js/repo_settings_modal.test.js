import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <div id="toast"></div>
    <div id="repo-shell"></div>
    <button id="repo-settings" type="button">Settings</button>
    <div id="repo-settings-modal" class="modal-overlay" hidden>
      <div class="modal-dialog" role="dialog">
        <button id="repo-settings-close" type="button">Close</button>
        <select id="repo-update-target">
          <option value="web">Web only</option>
        </select>
        <button id="repo-update-btn" type="button">Update</button>
        <select id="autorunner-model-select"></select>
        <select id="autorunner-effort-select"></select>
        <select id="autorunner-approval-select"></select>
        <select id="autorunner-sandbox-select"></select>
        <input id="autorunner-max-runs-input" type="number" />
        <input id="autorunner-network-toggle" type="checkbox" />
        <button id="autorunner-settings-save" type="button">Save</button>
        <button id="autorunner-settings-reload" type="button">Reload</button>
      </div>
    </div>
    <div id="thread-tools-list"></div>
    <button id="thread-new-autorunner" type="button"></button>
    <button id="thread-archive-autorunner" type="button"></button>
    <button id="thread-reset-all" type="button"></button>
    <a id="thread-backup-download"></a>
    <div id="confirm-modal" class="modal-overlay" hidden>
      <div class="modal-dialog" role="dialog">
        <div id="confirm-modal-message"></div>
        <button id="confirm-modal-ok" type="button">OK</button>
        <button id="confirm-modal-cancel" type="button">Cancel</button>
      </div>
    </div>
  </body></html>`,
  { url: "http://localhost/repos/demo/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLAnchorElement = dom.window.HTMLAnchorElement;
globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.Node = dom.window.Node;
globalThis.localStorage = dom.window.localStorage;
globalThis.sessionStorage = dom.window.sessionStorage;

const settingsModule = await import(
  "../../src/codex_autorunner/static/generated/settings.js"
);
const {
  initRepoSettingsPanel,
  openRepoSettings,
  __settingsTest,
} = settingsModule;
initRepoSettingsPanel();

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function optionValues(select) {
  return Array.from(select.options).map((option) => option.value);
}

async function flushUi(times = 6) {
  for (let idx = 0; idx < times; idx += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function resetDomState() {
  document.getElementById("repo-settings-modal").hidden = true;
  document.getElementById("confirm-modal").hidden = true;
  document.getElementById("thread-tools-list").innerHTML = "";
  document.getElementById("repo-update-target").replaceChildren();
  document.getElementById("repo-update-target").appendChild(
    Object.assign(document.createElement("option"), {
      value: "web",
      textContent: "Web only",
    })
  );
  document.getElementById("repo-update-target").dataset.updateTargetsInitialized = "";
  document.getElementById("autorunner-model-select").replaceChildren();
  document.getElementById("autorunner-effort-select").replaceChildren();
  document.getElementById("autorunner-approval-select").replaceChildren();
  document.getElementById("autorunner-sandbox-select").replaceChildren();
  document.getElementById("autorunner-max-runs-input").value = "";
  document.getElementById("autorunner-network-toggle").checked = false;
  document.getElementById("autorunner-network-toggle").indeterminate = false;
}

function installFetchMock({ sessionSettingsRef, updateTargets, modelCatalog, posts }) {
  globalThis.fetch = async (url, options = {}) => {
    const href = String(url);
    const method = String(options.method || "GET").toUpperCase();
    if (href.endsWith("/repos/demo/api/app-server/threads")) {
      return jsonResponse({ autorunner: "thread-123" });
    }
    if (href.endsWith("/repos/demo/api/templates/repos")) {
      return jsonResponse({ enabled: true, repos: [] });
    }
    if (href.endsWith("/repos/demo/system/update/targets")) {
      return jsonResponse(updateTargets);
    }
    if (href.endsWith("/repos/demo/api/session/settings") && method === "GET") {
      return jsonResponse(sessionSettingsRef.current);
    }
    if (href.endsWith("/repos/demo/api/session/settings") && method === "POST") {
      const body = JSON.parse(String(options.body || "{}"));
      posts.push(body);
      sessionSettingsRef.current = { ...body };
      return jsonResponse(sessionSettingsRef.current);
    }
    if (href.endsWith("/repos/demo/api/agents")) {
      return jsonResponse({
        default: "codex",
        agents: [{ id: "codex", capabilities: ["model_listing"] }],
      });
    }
    if (href.endsWith("/repos/demo/api/agents/codex/models")) {
      return jsonResponse(modelCatalog);
    }
    throw new Error(`Unexpected fetch: ${method} ${href}`);
  };
}

test("parsePositiveIntegerRuns accepts whole positive numbers only", () => {
  const p = __settingsTest.parsePositiveIntegerRuns;
  assert.equal(p(""), null);
  assert.equal(p("   "), null);
  assert.equal(p("7"), 7);
  assert.equal(p("01"), 1);
  assert.equal(p("1.5"), undefined);
  assert.equal(p("1e3"), undefined);
  assert.equal(p("0"), undefined);
  assert.equal(p("-2"), undefined);
});

test("repo settings modal hydrates update targets and autorunner controls", async () => {
  __settingsTest.reset();
  localStorage.clear();
  sessionStorage.clear();
  resetDomState();

  const posts = [];
  const sessionSettingsRef = {
    current: {
      autorunner_model_override: "gpt-5.4",
      autorunner_effort_override: "high",
      autorunner_approval_policy: "never",
      autorunner_sandbox_mode: "workspaceWrite",
      autorunner_workspace_write_network: true,
      runner_stop_after_runs: 3,
    },
  };
  installFetchMock({
    sessionSettingsRef,
    updateTargets: {
      targets: [
        { value: "all", label: "all", description: "Web + Telegram + Discord" },
        { value: "web", label: "web", description: "Web UI only" },
        { value: "chat", label: "chat", description: "Telegram + Discord" },
      ],
      default_target: "all",
    },
    modelCatalog: {
      default_model: "gpt-5.4",
      models: [
        {
          id: "gpt-5.4",
          display_name: "GPT-5.4",
          supports_reasoning: true,
          reasoning_options: ["medium", "high"],
        },
        {
          id: "gpt-5.2",
          display_name: "GPT-5.2",
          supports_reasoning: true,
          reasoning_options: ["medium"],
        },
      ],
    },
    posts,
  });

  openRepoSettings(document.getElementById("repo-settings"));
  await flushUi();

  assert.equal(document.getElementById("repo-settings-modal").hidden, false);
  assert.deepEqual(optionValues(document.getElementById("repo-update-target")), [
    "all",
    "web",
    "chat",
  ]);
  assert.match(
    document.getElementById("thread-tools-list").textContent || "",
    /thread-123/
  );
  assert.equal(__settingsTest.getCurrentCatalogAgent(), "codex");

  const modelSelect = document.getElementById("autorunner-model-select");
  const effortSelect = document.getElementById("autorunner-effort-select");
  const approvalSelect = document.getElementById("autorunner-approval-select");
  const sandboxSelect = document.getElementById("autorunner-sandbox-select");
  const maxRunsInput = document.getElementById("autorunner-max-runs-input");
  const networkToggle = document.getElementById("autorunner-network-toggle");

  assert.deepEqual(optionValues(modelSelect), ["", "gpt-5.4", "gpt-5.2"]);
  assert.equal(modelSelect.value, "gpt-5.4");
  assert.deepEqual(optionValues(effortSelect), ["", "medium", "high"]);
  assert.equal(effortSelect.value, "high");
  assert.equal(approvalSelect.value, "never");
  assert.equal(sandboxSelect.value, "workspaceWrite");
  assert.equal(maxRunsInput.value, "3");
  assert.equal(networkToggle.checked, true);
  assert.equal(networkToggle.indeterminate, false);

  modelSelect.value = "gpt-5.2";
  modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
  await flushUi(2);
  assert.deepEqual(optionValues(effortSelect), ["", "medium"]);
  effortSelect.value = "medium";
  approvalSelect.value = "unlessTrusted";
  sandboxSelect.value = "dangerFullAccess";
  maxRunsInput.value = "5";
  networkToggle.indeterminate = false;
  networkToggle.checked = false;

  document
    .getElementById("autorunner-settings-save")
    .dispatchEvent(new Event("click", { bubbles: true }));
  await flushUi();

  assert.deepEqual(posts, [
    {
      autorunner_model_override: "gpt-5.2",
      autorunner_effort_override: "medium",
      autorunner_approval_policy: "unlessTrusted",
      autorunner_sandbox_mode: "dangerFullAccess",
      autorunner_workspace_write_network: false,
      runner_stop_after_runs: 5,
    },
  ]);
});

test("repo settings save rejects non-integer max runs without posting", async () => {
  __settingsTest.reset();
  localStorage.clear();
  sessionStorage.clear();
  resetDomState();

  const posts = [];
  const sessionSettingsRef = {
    current: {
      autorunner_model_override: "gpt-5.4",
      autorunner_effort_override: "medium",
      autorunner_approval_policy: "never",
      autorunner_sandbox_mode: "workspaceWrite",
      autorunner_workspace_write_network: false,
      runner_stop_after_runs: 3,
    },
  };
  installFetchMock({
    sessionSettingsRef,
    updateTargets: {
      targets: [{ value: "web", label: "web", description: "Web UI only" }],
      default_target: "web",
    },
    modelCatalog: {
      default_model: "gpt-5.4",
      models: [
        {
          id: "gpt-5.4",
          display_name: "GPT-5.4",
          supports_reasoning: true,
          reasoning_options: ["medium", "high"],
        },
      ],
    },
    posts,
  });

  openRepoSettings(document.getElementById("repo-settings"));
  await flushUi();

  document.getElementById("autorunner-max-runs-input").value = "1.5";

  document
    .getElementById("autorunner-settings-save")
    .dispatchEvent(new Event("click", { bubbles: true }));
  await flushUi();

  assert.deepEqual(posts, []);
  assert.match(
    document.getElementById("toast").textContent || "",
    /positive whole number/i
  );
});

test("repo settings reload refreshes session values and preserves default network state", async () => {
  __settingsTest.reset();
  localStorage.clear();
  sessionStorage.clear();
  resetDomState();

  const posts = [];
  const sessionSettingsRef = {
    current: {
      autorunner_model_override: null,
      autorunner_effort_override: null,
      autorunner_approval_policy: null,
      autorunner_sandbox_mode: null,
      autorunner_workspace_write_network: null,
      runner_stop_after_runs: null,
    },
  };
  installFetchMock({
    sessionSettingsRef,
    updateTargets: {
      targets: [{ value: "web", label: "web", description: "Web UI only" }],
      default_target: "web",
    },
    modelCatalog: {
      default_model: "gpt-5.4",
      models: [
        {
          id: "gpt-5.4",
          display_name: "GPT-5.4",
          supports_reasoning: true,
          reasoning_options: ["medium", "high"],
        },
      ],
    },
    posts,
  });

  openRepoSettings(document.getElementById("repo-settings"));
  await flushUi();

  const modelSelect = document.getElementById("autorunner-model-select");
  const approvalSelect = document.getElementById("autorunner-approval-select");
  const sandboxSelect = document.getElementById("autorunner-sandbox-select");
  const maxRunsInput = document.getElementById("autorunner-max-runs-input");
  const networkToggle = document.getElementById("autorunner-network-toggle");

  assert.equal(modelSelect.value, "");
  assert.equal(approvalSelect.value, "");
  assert.equal(sandboxSelect.value, "");
  assert.equal(maxRunsInput.value, "");
  assert.equal(networkToggle.checked, false);
  assert.equal(networkToggle.indeterminate, true);

  sessionSettingsRef.current = {
    autorunner_model_override: "gpt-5.4",
    autorunner_effort_override: "medium",
    autorunner_approval_policy: "never",
    autorunner_sandbox_mode: "workspaceWrite",
    autorunner_workspace_write_network: false,
    runner_stop_after_runs: 7,
  };

  document
    .getElementById("autorunner-settings-reload")
    .dispatchEvent(new Event("click", { bubbles: true }));
  await flushUi();

  assert.equal(modelSelect.value, "gpt-5.4");
  assert.equal(document.getElementById("autorunner-effort-select").value, "medium");
  assert.equal(approvalSelect.value, "never");
  assert.equal(sandboxSelect.value, "workspaceWrite");
  assert.equal(maxRunsInput.value, "7");
  assert.equal(networkToggle.checked, false);
  assert.equal(networkToggle.indeterminate, false);
  assert.deepEqual(posts, []);
});

test("repo settings keeps save disabled when settings load fails", async () => {
  __settingsTest.reset();
  localStorage.clear();
  sessionStorage.clear();
  resetDomState();

  const posts = [];
  globalThis.fetch = async (url, options = {}) => {
    const href = String(url);
    const method = String(options.method || "GET").toUpperCase();
    if (href.endsWith("/repos/demo/api/app-server/threads")) {
      return jsonResponse({ autorunner: "thread-123" });
    }
    if (href.endsWith("/repos/demo/api/templates/repos")) {
      return jsonResponse({ enabled: true, repos: [] });
    }
    if (href.endsWith("/repos/demo/system/update/targets")) {
      return jsonResponse({
        targets: [{ value: "web", label: "web", description: "Web UI only" }],
        default_target: "web",
      });
    }
    if (href.endsWith("/repos/demo/api/session/settings") && method === "GET") {
      return jsonResponse({ detail: "load failed" }, 500);
    }
    if (href.endsWith("/repos/demo/api/session/settings") && method === "POST") {
      posts.push(JSON.parse(String(options.body || "{}")));
      return jsonResponse({});
    }
    if (href.endsWith("/repos/demo/api/agents")) {
      return jsonResponse({
        default: "codex",
        agents: [{ id: "codex", capabilities: ["model_listing"] }],
      });
    }
    if (href.endsWith("/repos/demo/api/agents/codex/models")) {
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
    throw new Error(`Unexpected fetch: ${method} ${href}`);
  };

  openRepoSettings(document.getElementById("repo-settings"));
  await flushUi();

  const saveBtn = document.getElementById("autorunner-settings-save");
  assert.equal(saveBtn.disabled, true);

  saveBtn.dispatchEvent(new Event("click", { bubbles: true }));
  await flushUi(2);

  assert.deepEqual(posts, []);
});
