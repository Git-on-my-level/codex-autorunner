import { api, flash, statusPill, confirmModal } from "./utils.js";
import { subscribe } from "./bus.js";
import {
  loadState,
  startRun,
  stopRun,
  resumeRun,
  killRun,
  resetRunner,
  startStatePolling,
} from "./state.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";

function renderState(state) {
  if (!state) return;
  statusPill(document.getElementById("runner-status"), state.status);
  document.getElementById("last-run-id").textContent = state.last_run_id ?? "–";
  document.getElementById("last-exit-code").textContent =
    state.last_exit_code ?? "–";
  document.getElementById("last-start").textContent =
    state.last_run_started_at ?? "–";
  document.getElementById("last-finish").textContent =
    state.last_run_finished_at ?? "–";
  document.getElementById("todo-count").textContent =
    state.outstanding_count ?? "–";
  document.getElementById("done-count").textContent = state.done_count ?? "–";
  document.getElementById("runner-pid").textContent = `Runner pid: ${
    state.runner_pid ?? "–"
  }`;
}

function setUsageLoading(loading) {
  const btn = document.getElementById("usage-refresh");
  if (!btn) return;
  btn.disabled = loading;
  btn.classList.toggle("loading", loading);
}

function formatTokensCompact(val) {
  if (val === null || val === undefined) return "–";
  const num = Number(val);
  if (Number.isNaN(num)) return val;
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(0)}k`;
  return num.toLocaleString();
}

function renderUsage(data) {
  const totals = data?.totals || {};
  const events = data?.events ?? 0;
  const rate = data?.latest_rate_limits;
  const codexHome = data?.codex_home || "–";

  const eventsEl = document.getElementById("usage-events");
  if (eventsEl) {
    eventsEl.textContent = `${events} ev`;
  }
  const totalEl = document.getElementById("usage-total");
  const inputEl = document.getElementById("usage-input");
  const cachedEl = document.getElementById("usage-cached");
  const outputEl = document.getElementById("usage-output");
  const reasoningEl = document.getElementById("usage-reasoning");
  const ratesEl = document.getElementById("usage-rates");
  const metaEl = document.getElementById("usage-meta");

  if (totalEl) totalEl.textContent = formatTokensCompact(totals.total_tokens);
  if (inputEl) inputEl.textContent = formatTokensCompact(totals.input_tokens);
  if (cachedEl)
    cachedEl.textContent = formatTokensCompact(totals.cached_input_tokens);
  if (outputEl)
    outputEl.textContent = formatTokensCompact(totals.output_tokens);
  if (reasoningEl)
    reasoningEl.textContent = formatTokensCompact(
      totals.reasoning_output_tokens
    );

  if (ratesEl) {
    if (rate) {
      const primary = rate.primary || {};
      const secondary = rate.secondary || {};
      ratesEl.textContent = `${primary.used_percent ?? "–"}%/${
        primary.window_minutes ?? ""
      }m · ${secondary.used_percent ?? "–"}%/${
        secondary.window_minutes ?? ""
      }m`;
    } else {
      ratesEl.textContent = "–";
    }
  }
  if (metaEl) metaEl.textContent = codexHome;
}

async function loadUsage() {
  setUsageLoading(true);
  try {
    const data = await api("/api/usage");
    renderUsage(data);
  } catch (err) {
    renderUsage(null);
    flash(err.message || "Failed to load usage", "error");
  } finally {
    setUsageLoading(false);
  }
}

function bindAction(buttonId, action) {
  const btn = document.getElementById(buttonId);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      await action();
    } catch (err) {
      flash(err.message);
    } finally {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  });
}

export function initDashboard() {
  subscribe("state:update", renderState);
  bindAction("start-run", () => startRun(false));
  bindAction("start-once", () => startRun(true));
  bindAction("stop-run", stopRun);
  bindAction("resume-run", resumeRun);
  bindAction("kill-run", killRun);
  bindAction("reset-runner", async () => {
    const confirmed = await confirmModal(
      "Reset runner? This will clear all logs and reset run ID to 1."
    );
    if (confirmed) await resetRunner();
  });
  bindAction("refresh-state", loadState);
  bindAction("usage-refresh", loadUsage);

  // Initial load
  loadUsage();
  startStatePolling();

  // Register auto-refresh for usage data (every 60s, only when dashboard tab is active)
  registerAutoRefresh("dashboard-usage", {
    callback: loadUsage,
    tabId: "dashboard",
    interval: CONSTANTS.UI.AUTO_REFRESH_USAGE_INTERVAL,
    refreshOnActivation: true,
    immediate: false, // Already called loadUsage() above
  });
}
