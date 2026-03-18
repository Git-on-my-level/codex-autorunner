import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <div id="hub-shell">
      <section id="hub-repo-panel" class="hub-repo-panel">
        <div class="hub-panel-header">
          <div class="hub-panel-header-main">
            <button id="hub-repo-panel-summary" aria-controls="hub-repo-list" aria-expanded="true"></button>
            <span id="hub-repo-panel-state"></span>
            <div class="hub-repo-controls">
              <button id="hub-cleanup-all-threads" type="button"></button>
            </div>
          </div>
        </div>
        <div id="hub-repo-list"></div>
      </section>
      <section id="hub-agent-panel" class="hub-repo-panel hub-agent-panel">
        <div class="hub-panel-header">
          <div class="hub-panel-header-main">
            <button id="hub-agent-panel-summary" aria-controls="hub-agent-workspace-list" aria-expanded="false"></button>
            <span id="hub-agent-panel-state"></span>
          </div>
        </div>
        <div id="hub-agent-workspace-list"></div>
      </section>
    </div>
    <div id="hub-last-scan"></div>
    <div id="pma-last-scan"></div>
    <div id="hub-count-total"></div>
    <div id="hub-count-running"></div>
    <div id="hub-count-missing"></div>
    <div id="hub-usage-meta"></div>
    <button id="hub-usage-refresh"></button>
    <div id="hub-version"></div>
    <div id="pma-version"></div>
    <input id="hub-repo-search" value="" />
    <select id="hub-flow-filter"></select>
    <select id="hub-sort-order"></select>
    <button id="hub-new-agent" type="button"></button>
    <div id="create-agent-workspace-modal" class="modal-overlay" hidden>
      <div class="modal-dialog" role="dialog">
        <input id="create-agent-workspace-id" />
        <input id="create-agent-workspace-runtime" />
        <input id="create-agent-workspace-name" />
        <button id="create-agent-workspace-cancel" type="button"></button>
        <button id="create-agent-workspace-submit" type="button"></button>
      </div>
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
try {
  globalThis.navigator = dom.window.navigator;
} catch {
  // Node 24+ exposes a read-only navigator.
}

const { __hubTest } = await import(
  "../../src/codex_autorunner/static/generated/hub.js"
);

test("repo cards show chat binding labels instead of raw chat ids", () => {
  const lastActiveAt = new Date(Date.now() - 13 * 60 * 1000 - 5000).toISOString();
  __hubTest.setHubChannelEntries([
    {
      key: "discord:ce806ba9-4e19-459a-9e01-2d3d3c6eafd4",
      repo_id: "stablecoin-engine",
      source: "discord",
      display: "Personal Workspace / #car-1",
      seen_at: lastActiveAt,
    },
  ]);

  __hubTest.renderRepos([
    {
      id: "stablecoin-engine",
      path: "/tmp/stablecoin-engine",
      display_name: "stablecoin-engine",
      enabled: true,
      auto_run: false,
      worktree_setup_commands: [],
      kind: "base",
      worktree_of: null,
      branch: "main",
      exists_on_disk: true,
      is_clean: true,
      initialized: true,
      init_error: null,
      status: "running",
      lock_status: "unlocked",
      last_run_id: "ce806ba9-4e19-459a-9e01-2d3d3c6eafd4",
      last_exit_code: null,
      last_run_started_at: lastActiveAt,
      last_run_finished_at: lastActiveAt,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      cleanup_blocked_by_chat_binding: false,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /Discord/);
  assert.match(text, /Personal Workspace \/ #car-1/);
  assert.match(text, /13m ago/);
  assert.doesNotMatch(text, /ce806ba9-4e19-459a-9e01-2d3d3c6eafd4/);
});

test("worktree cards show archive state action when CAR state is present", () => {
  __hubTest.setHubChannelEntries([]);
  __hubTest.renderRepos([
    {
      id: "base--feature",
      path: "/tmp/base--feature",
      display_name: "base--feature",
      enabled: true,
      auto_run: false,
      worktree_setup_commands: [],
      kind: "worktree",
      worktree_of: "base",
      branch: "feature",
      exists_on_disk: true,
      is_clean: true,
      initialized: true,
      init_error: null,
      status: "idle",
      lock_status: "unlocked",
      last_run_id: null,
      last_exit_code: null,
      last_run_started_at: null,
      last_run_finished_at: null,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      cleanup_blocked_by_chat_binding: false,
      has_car_state: true,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /Archive state/);
  assert.match(text, /Cleanup/);
});

test("base repo cards show archive state and cleanup threads actions", () => {
  __hubTest.setHubChannelEntries([]);
  __hubTest.renderRepos([
    {
      id: "base",
      path: "/tmp/base",
      display_name: "base",
      enabled: true,
      auto_run: false,
      worktree_setup_commands: [],
      kind: "base",
      worktree_of: null,
      branch: "main",
      exists_on_disk: true,
      is_clean: true,
      initialized: true,
      init_error: null,
      status: "idle",
      lock_status: "unlocked",
      last_run_id: null,
      last_exit_code: null,
      last_run_started_at: null,
      last_run_finished_at: null,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      unbound_managed_thread_count: 2,
      cleanup_blocked_by_chat_binding: false,
      has_car_state: true,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /Archive state/);
  assert.match(text, /Cleanup threads \(2\)/);
  const cleanupAllText =
    document.getElementById("hub-cleanup-all-threads")?.textContent || "";
  assert.match(cleanupAllText, /Cleanup all \(2\)/);
});

test("repo cards hide duplicate chat-bound pma thread labels", () => {
  const now = new Date().toISOString();
  __hubTest.setHubChannelEntries([
    {
      key: "discord:1234567890",
      repo_id: "stablecoin-engine",
      source: "discord",
      display: "Personal Workspace / #car-1",
      seen_at: now,
    },
    {
      key: "pma_thread:dup",
      repo_id: "stablecoin-engine",
      source: "pma_thread",
      display: "discord:1234567890",
      seen_at: now,
      provenance: {
        source: "pma_thread",
        managed_thread_id: "dup",
        thread_kind: "interactive",
      },
    },
    {
      key: "pma_thread:flow",
      repo_id: "stablecoin-engine",
      source: "pma_thread",
      display: "ticket-flow:codex",
      seen_at: now,
      provenance: {
        source: "pma_thread",
        managed_thread_id: "flow",
        thread_kind: "ticket_flow",
      },
    },
  ]);

  __hubTest.renderRepos([
    {
      id: "stablecoin-engine",
      path: "/tmp/stablecoin-engine",
      display_name: "stablecoin-engine",
      enabled: true,
      auto_run: false,
      worktree_setup_commands: [],
      kind: "base",
      worktree_of: null,
      branch: "main",
      exists_on_disk: true,
      is_clean: true,
      initialized: true,
      init_error: null,
      status: "running",
      lock_status: "unlocked",
      last_run_id: "run-1",
      last_exit_code: null,
      last_run_started_at: now,
      last_run_finished_at: now,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      cleanup_blocked_by_chat_binding: false,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /Personal Workspace \/ #car-1/);
  assert.match(text, /Ticket flow/);
  assert.doesNotMatch(text, /\bdiscord:1234567890\b/);
});

test("repo cards collapse pma-managed threads into a compact summary row", () => {
  const now = new Date().toISOString();
  __hubTest.setHubChannelEntries([
    {
      key: "discord:repo-main",
      repo_id: "stablecoin-engine",
      source: "discord",
      display: "Personal Workspace / #car-1",
      seen_at: now,
    },
    {
      key: "pma_thread:one",
      repo_id: "stablecoin-engine",
      source: "pma_thread",
      display: "ticket-flow:codex",
      seen_at: now,
      provenance: {
        source: "pma_thread",
        managed_thread_id: "one",
        thread_kind: "ticket_flow",
      },
    },
    {
      key: "pma_thread:two",
      repo_id: "stablecoin-engine",
      source: "pma_thread",
      display: "pma:codex",
      seen_at: now,
      provenance: {
        source: "pma_thread",
        managed_thread_id: "two",
        thread_kind: "ticket_flow",
      },
    },
    {
      key: "pma_thread:three",
      repo_id: "stablecoin-engine",
      source: "pma_thread",
      display: "pma:codex",
      seen_at: now,
      provenance: {
        source: "pma_thread",
        managed_thread_id: "three",
        thread_kind: "interactive",
      },
    },
  ]);

  __hubTest.renderRepos([
    {
      id: "stablecoin-engine",
      path: "/tmp/stablecoin-engine",
      display_name: "stablecoin-engine",
      enabled: true,
      auto_run: false,
      worktree_setup_commands: [],
      kind: "base",
      worktree_of: null,
      branch: "main",
      exists_on_disk: true,
      is_clean: true,
      initialized: true,
      init_error: null,
      status: "running",
      lock_status: "unlocked",
      last_run_id: "run-1",
      last_exit_code: null,
      last_run_started_at: now,
      last_run_finished_at: now,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      cleanup_blocked_by_chat_binding: false,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /Personal Workspace \/ #car-1/);
  assert.match(text, /Ticket flow/);
  assert.match(text, /x2/);
  assert.match(text, /Agent thread/);
  assert.doesNotMatch(text, /ticket-flow:codex/);
});

test("hub panel state collapses repositories and agents independently with one expanded panel", () => {
  __hubTest.applyHubPanelState("agents");

  const repoPanel = document.getElementById("hub-repo-panel");
  const agentPanel = document.getElementById("hub-agent-panel");
  const repoToggle = document.getElementById("hub-repo-panel-summary");
  const agentToggle = document.getElementById("hub-agent-panel-summary");

  assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), true);
  assert.equal(agentPanel?.classList.contains("hub-panel-collapsed"), false);
  assert.equal(repoToggle?.getAttribute("aria-expanded"), "false");
  assert.equal(agentToggle?.getAttribute("aria-expanded"), "true");
});

test("hub panel toggles keep working when localStorage is unavailable", () => {
  const originalGetItem = globalThis.localStorage.getItem.bind(globalThis.localStorage);
  const originalSetItem = globalThis.localStorage.setItem.bind(globalThis.localStorage);
  globalThis.localStorage.getItem = () => {
    throw new Error("blocked");
  };
  globalThis.localStorage.setItem = () => {
    throw new Error("blocked");
  };

  try {
    const repoPanel = document.getElementById("hub-repo-panel");
    const agentPanel = document.getElementById("hub-agent-panel");

    __hubTest.applyHubPanelState("repos");
    __hubTest.toggleHubPanel("agents");
    assert.equal(agentPanel?.classList.contains("hub-panel-collapsed"), false);
    assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), true);

    __hubTest.toggleHubPanel("agents");
    assert.equal(agentPanel?.classList.contains("hub-panel-collapsed"), false);

    __hubTest.toggleHubPanel("repos");
    assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), false);
  } finally {
    globalThis.localStorage.getItem = originalGetItem;
    globalThis.localStorage.setItem = originalSetItem;
  }
});

test("agent workspace cards render runtime, managed path, and lifecycle actions", () => {
  __hubTest.renderAgentWorkspaces([
    {
      id: "zc-main",
      runtime: "zeroclaw",
      path: ".codex-autorunner/runtimes/zeroclaw/zc-main",
      display_name: "ZeroClaw Main",
      enabled: false,
      exists_on_disk: true,
      effective_destination: {
        kind: "docker",
        image: "ghcr.io/acme/zeroclaw:latest",
      },
      resource_kind: "agent_workspace",
    },
  ]);

  const text =
    document.getElementById("hub-agent-workspace-list")?.textContent || "";
  assert.match(text, /ZeroClaw Main/);
  assert.match(text, /zeroclaw/);
  assert.match(text, /disabled/);
  assert.match(text, /Destination/);
  assert.match(text, /Remove/);
  assert.match(text, /Delete/);
  assert.match(text, /zc-main/);
});

test("hub interaction harness expands agents and opens the agent modal", () => {
  const agentPanel = document.getElementById("hub-agent-panel");
  const repoPanel = document.getElementById("hub-repo-panel");
  const agentSummary = document.getElementById("hub-agent-panel-summary");
  const newAgentBtn = document.getElementById("hub-new-agent");
  const agentModal = document.getElementById("create-agent-workspace-modal");

  agentModal.hidden = true;
  __hubTest.applyHubPanelState("repos");
  __hubTest.initInteractionHarness();

  agentSummary.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  assert.equal(agentPanel?.classList.contains("hub-panel-collapsed"), false);
  assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), true);

  newAgentBtn.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  assert.equal(agentPanel?.classList.contains("hub-panel-collapsed"), false);
  assert.equal(agentModal?.hidden, false);
});
