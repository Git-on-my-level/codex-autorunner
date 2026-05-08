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
              <button id="hub-cleanup-all" type="button"></button>
            </div>
          </div>
        </div>
        <div id="hub-repo-list"></div>
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

test("worktree cards show archive action when CAR state is present", () => {
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
  assert.match(text, /Archive/);
  assert.match(text, /Cleanup/);
});

test("base repo cards show archive action and cleanup-all summary", () => {
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
  assert.match(text, /Archive/);
  assert.doesNotMatch(text, /Cleanup threads/);
  const cleanupAllText =
    document.getElementById("hub-cleanup-all")?.textContent || "";
  assert.equal(cleanupAllText.trim(), "Cleanup all");
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

test("hub panel state expands repositories when repos panel is active", () => {
  __hubTest.applyHubPanelState("repos");

  const repoPanel = document.getElementById("hub-repo-panel");
  const repoToggle = document.getElementById("hub-repo-panel-summary");

  assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), false);
  assert.equal(repoToggle?.getAttribute("aria-expanded"), "true");
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

    __hubTest.applyHubPanelState("repos");
    assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), false);

    __hubTest.toggleHubPanel("agents");
    assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), true);

    __hubTest.toggleHubPanel("repos");
    assert.equal(repoPanel?.classList.contains("hub-panel-collapsed"), false);
  } finally {
    globalThis.localStorage.getItem = originalGetItem;
    globalThis.localStorage.setItem = originalSetItem;
  }
});

test("hub bootstrap cache falls back to durable localStorage and restores session cache", () => {
  sessionStorage.clear();
  localStorage.clear();

  const payload = {
    repos: [
      {
        id: "cached-repo",
        path: "/tmp/cached-repo",
        display_name: "cached-repo",
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
        cleanup_blocked_by_chat_binding: false,
        ticket_flow: null,
        ticket_flow_display: null,
      },
    ],
    last_scan_at: "2026-04-10T10:00:00Z",
    pinned_parent_repo_ids: [],
  };

  __hubTest.saveHubBootstrapCache(payload);
  sessionStorage.clear();

  const restored = __hubTest.loadHubBootstrapCache();
  const sessionValues = Array.from(
    { length: sessionStorage.length },
    (_, index) => sessionStorage.getItem(sessionStorage.key(index) || "") || ""
  );

  assert.deepEqual(restored, payload);
  assert.equal(sessionValues.some((value) => value.includes("cached-repo")), true);
});

test("pinned repos sort before unpinned repos in renderRepos source", async () => {
  const fs = await import("node:fs");
  const path = await import("node:path");

  const repoCardsSource = fs.readFileSync(
    path.join(process.cwd(), "src", "codex_autorunner", "static_src", "hubRepoCards.ts"),
    "utf8"
  );

  const sortStart = repoCardsSource.indexOf("orderedGroups = groups");
  assert.ok(sortStart !== -1, "expected orderedGroups sort in hubRepoCards");

  const pinnedSort = repoCardsSource.indexOf("if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;", sortStart);
  assert.ok(pinnedSort !== -1, "expected pinned-first sort comparison in hubRepoCards");

  const filtersSource = fs.readFileSync(
    path.join(process.cwd(), "src", "codex_autorunner", "static_src", "hubFilters.ts"),
    "utf8"
  );

  const pinnedSetInBuild = filtersSource.indexOf("pinned: pinnedParentRepoIds.has(group.base.id)");
  assert.ok(pinnedSetInBuild !== -1, "expected pinned flag from pinnedParentRepoIds in hubFilters");
});

test("hub bootstrap cache does not act as authoritative source for repo state", () => {
  sessionStorage.clear();
  localStorage.clear();

  const stalePayload = {
    repos: [
      {
        id: "stale-repo",
        path: "/tmp/stale-repo",
        display_name: "stale-repo",
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
        cleanup_blocked_by_chat_binding: false,
        ticket_flow: null,
        ticket_flow_display: null,
      },
    ],
    last_scan_at: "2026-01-01T00:00:00Z",
    pinned_parent_repo_ids: [],
  };

  __hubTest.saveHubBootstrapCache(stalePayload);

  const freshPayload = {
    repos: [
      {
        id: "fresh-repo",
        path: "/tmp/fresh-repo",
        display_name: "fresh-repo",
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
        last_run_id: "run-fresh",
        last_exit_code: null,
        last_run_started_at: new Date().toISOString(),
        last_run_finished_at: null,
        runner_pid: 12345,
        effective_destination: { kind: "local" },
        mounted: false,
        mount_error: null,
        cleanup_blocked_by_chat_binding: false,
        ticket_flow: null,
        ticket_flow_display: null,
      },
    ],
    last_scan_at: new Date().toISOString(),
    pinned_parent_repo_ids: [],
  };

  __hubTest.renderRepos(freshPayload.repos);

  const text = document.getElementById("hub-repo-list")?.textContent || "";
  assert.match(text, /fresh-repo/);
  assert.doesNotMatch(text, /stale-repo/);
});

test("cleanup-all button reflects aria-disabled when no eligible cleanup targets exist", () => {
  __hubTest.setHubChannelEntries([]);
  __hubTest.renderRepos([
    {
      id: "active-base",
      path: "/tmp/active-base",
      display_name: "active-base",
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
      last_run_id: null,
      last_exit_code: null,
      last_run_started_at: null,
      last_run_finished_at: null,
      runner_pid: null,
      effective_destination: { kind: "local" },
      mounted: false,
      mount_error: null,
      cleanup_blocked_by_chat_binding: false,
      ticket_flow: null,
      ticket_flow_display: null,
    },
  ]);

  const btn = document.getElementById("hub-cleanup-all");
  assert.equal(btn?.getAttribute("aria-disabled"), "true");
});

test("hub interaction harness initializes repo panel controls", () => {
  __hubTest.applyHubPanelState("repos");
  assert.doesNotThrow(() => __hubTest.initInteractionHarness());
});
