import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <div id="hub-repo-list"></div>
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
