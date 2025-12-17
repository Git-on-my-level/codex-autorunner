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

  // Show "Summary" CTA when TODO is fully complete.
  const summaryBtn = document.getElementById("open-summary");
  if (summaryBtn) {
    const done = Number(state.outstanding_count ?? NaN) === 0;
    summaryBtn.classList.toggle("hidden", !done);
  }
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

function renderUsageProgressBar(container, percent, windowMinutes) {
  if (!container) return;
  
  const pct = typeof percent === "number" ? Math.min(100, Math.max(0, percent)) : 0;
  const hasData = typeof percent === "number";
  
  // Determine color based on percentage
  let barClass = "usage-bar-ok";
  if (pct >= 90) barClass = "usage-bar-critical";
  else if (pct >= 70) barClass = "usage-bar-warning";
  
  container.innerHTML = `
    <div class="usage-progress-bar ${hasData ? "" : "usage-progress-bar-empty"}">
      <div class="usage-progress-fill ${barClass}" style="width: ${pct}%"></div>
    </div>
    <span class="usage-progress-label">${hasData ? `${pct}%` : "–"}${windowMinutes ? `/${windowMinutes}m` : ""}</span>
  `;
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
  const primaryBarEl = document.getElementById("usage-rate-primary");
  const secondaryBarEl = document.getElementById("usage-rate-secondary");

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

  // Render progress bars for rate limits
  if (rate) {
    const primary = rate.primary || {};
    const secondary = rate.secondary || {};
    
    renderUsageProgressBar(primaryBarEl, primary.used_percent, primary.window_minutes);
    renderUsageProgressBar(secondaryBarEl, secondary.used_percent, secondary.window_minutes);
    
    // Also update text fallback
    if (ratesEl) {
      ratesEl.textContent = `${primary.used_percent ?? "–"}%/${
        primary.window_minutes ?? ""
      }m · ${secondary.used_percent ?? "–"}%/${
        secondary.window_minutes ?? ""
      }m`;
    }
  } else {
    renderUsageProgressBar(primaryBarEl, null, null);
    renderUsageProgressBar(secondaryBarEl, null, null);
    if (ratesEl) ratesEl.textContent = "–";
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

async function handleSystemUpdate(btnId) {
  const btn = document.getElementById(btnId);
  if (!btn) return;

  const confirmed = await confirmModal(
    "Update Codex Autorunner? The service will restart."
  );
  if (!confirmed) return;

  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "Updating...";

  try {
    const res = await api("/system/update", { method: "POST" });
    flash(res.message || "Update started. Reloading...", "success");
    // Disable interaction
    document.body.style.pointerEvents = "none";
    // Wait for restart (approx 5-10s) then reload
    setTimeout(() => {
      window.location.reload();
    }, 8000);
  } catch (err) {
    flash(err.message || "Update failed", "error");
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function initSettings() {
  const settingsBtn = document.getElementById("repo-settings");
  const modal = document.getElementById("repo-settings-modal");
  const closeBtn = document.getElementById("repo-settings-close");
  const updateBtn = document.getElementById("repo-update-btn");

  if (settingsBtn && modal) {
    settingsBtn.addEventListener("click", () => {
      modal.hidden = false;
    });
  }

  if (closeBtn && modal) {
    closeBtn.addEventListener("click", () => {
      modal.hidden = true;
    });
  }

  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.hidden = true;
    });
  }

  if (updateBtn) {
    updateBtn.addEventListener("click", () =>
      handleSystemUpdate("repo-update-btn")
    );
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
  initSettings();
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

  const summaryBtn = document.getElementById("open-summary");
  if (summaryBtn) {
    summaryBtn.addEventListener("click", () => {
      const docsTab = document.querySelector('.tab[data-target="docs"]');
      if (docsTab) docsTab.click();
      const summaryChip = document.querySelector('.chip[data-doc="summary"]');
      if (summaryChip) summaryChip.click();
    });
  }

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
