import { api, flash, statusPill, confirmModal } from "./utils.js";
import { subscribe } from "./bus.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { renderTodoPreview } from "./todoPreview.js";
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

const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
let pendingSummaryOpen = false;
const usageChartState = {
  segment: "none",
  bucket: "day",
  windowDays: 30,
};
let usageSeriesRetryTimer = null;

function renderState(state) {
  if (!state) return;
  saveToCache("state", state);
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
  const modelEl = document.getElementById("runner-model");
  if (modelEl) modelEl.textContent = state.codex_model || "auto";

  // Show "Summary" CTA when TODO is fully complete.
  const summaryBtn = document.getElementById("open-summary");
  if (summaryBtn) {
    const done = Number(state.outstanding_count ?? NaN) === 0;
    summaryBtn.classList.toggle("hidden", !done);
  }
}

function updateTodoPreview(content) {
  renderTodoPreview(content || "");
  if (content !== undefined) {
    saveToCache("todo-doc", content || "");
  }
}

function handleDocsEvent(payload) {
  if (!payload) return;
  if (payload.kind === "todo") {
    updateTodoPreview(payload.content || "");
    return;
  }
  if (typeof payload.todo === "string") {
    updateTodoPreview(payload.todo);
  }
}

async function loadTodoPreview() {
  try {
    const data = await api("/api/docs");
    updateTodoPreview(data?.todo || "");
  } catch (err) {
    flash(err.message || "Failed to load TODO preview", "error");
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

function formatTokensAxis(val) {
  if (val === null || val === undefined) return "0";
  const num = Number(val);
  if (Number.isNaN(num)) return "0";
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return Math.round(num).toString();
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
  if (data) saveToCache("usage", data);
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

function buildUsageSeriesQuery() {
  const params = new URLSearchParams();
  const now = new Date();
  const since = new Date(now.getTime() - usageChartState.windowDays * 86400000);
  const bucket =
    usageChartState.windowDays >= 180 ? "week" : usageChartState.bucket;
  params.set("since", since.toISOString());
  params.set("until", now.toISOString());
  params.set("bucket", bucket);
  params.set("segment", usageChartState.segment);
  return params.toString();
}

function renderUsageChart(data) {
  const container = document.getElementById("usage-chart-canvas");
  if (!container) return;
  const buckets = data?.buckets || [];
  const series = data?.series || [];
  const isLoading = data?.status === "loading";
  if (!buckets.length || !series.length) {
    container.__usageChartBound = false;
    container.innerHTML = isLoading
      ? '<div class="usage-chart-empty">Loading…</div>'
      : '<div class="usage-chart-empty">No data</div>';
    return;
  }

  const width = 320;
  const height = 88;
  const padding = 8;
  const chartWidth = width - padding * 2;
  const chartHeight = height - padding * 2;
  const colors = [
    "#6cf5d8",
    "#6ca8ff",
    "#f5b86c",
    "#f56c8a",
    "#84d1ff",
    "#9be26f",
    "#f2a0c5",
  ];

  let scaleMax = 1;
  if (usageChartState.segment === "none") {
    const values = series[0]?.values || [];
    scaleMax = Math.max(...values, 1);
  } else {
    const totals = new Array(buckets.length).fill(0);
    series.forEach((entry) => {
      (entry.values || []).forEach((value, i) => {
        totals[i] += value;
      });
    });
    scaleMax = Math.max(...totals, 1);
  }

  const xFor = (index, count) => {
    if (count <= 1) return padding + chartWidth / 2;
    return padding + (index / (count - 1)) * chartWidth;
  };
  const yFor = (value) =>
    padding + chartHeight - (value / scaleMax) * chartHeight;

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Token usage trend">`;
  svg += `
    <defs>
      <linearGradient id="usage-line-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#6cf5d8" stop-opacity="0.35" />
        <stop offset="100%" stop-color="#6cf5d8" stop-opacity="0" />
      </linearGradient>
      <filter id="usage-line-glow" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="2" result="blur" />
        <feMerge>
          <feMergeNode in="blur" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
    </defs>
  `;

  const gridLines = 3;
  for (let i = 1; i <= gridLines; i += 1) {
    const y = padding + (chartHeight / (gridLines + 1)) * i;
    svg += `<line x1="${padding}" y1="${y}" x2="${
      padding + chartWidth
    }" y2="${y}" stroke="rgba(108, 245, 216, 0.12)" stroke-width="1" />`;
  }

  const maxLabel = formatTokensAxis(scaleMax);
  const midLabel = formatTokensAxis(scaleMax / 2);
  svg += `<text x="${padding}" y="${padding + 10}" fill="rgba(203, 213, 225, 0.7)" font-size="8">${maxLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight / 2 + 4
  }" fill="rgba(203, 213, 225, 0.6)" font-size="8">${midLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight + 2
  }" fill="rgba(203, 213, 225, 0.5)" font-size="8">0</text>`;

  if (usageChartState.segment === "none") {
    const values = series[0]?.values || [];
    const points = values.map((value, i) => {
      const x = xFor(i, values.length);
      const y = yFor(value);
      return `${x},${y}`;
    });
    if (values.length) {
      const x0 = xFor(0, values.length);
      const y0 = yFor(values[0] || 0);
      const linePath = `M ${points.join(" L ")}`;
      const areaPath = `${linePath} L ${
        padding + chartWidth
      },${padding + chartHeight} L ${padding},${
        padding + chartHeight
      } Z`;
      svg += `<path d="${areaPath}" fill="url(#usage-line-fill)" />`;
      svg += `<path d="${linePath}" fill="none" stroke="#6cf5d8" stroke-width="2" filter="url(#usage-line-glow)" />`;
      svg += `<circle cx="${x0}" cy="${y0}" r="2" fill="#6cf5d8" />`;
    }
  } else {
    const count = buckets.length;
    const accum = new Array(count).fill(0);
    series.forEach((entry, idx) => {
      const values = entry.values || [];
      const top = values.map((value, i) => {
        accum[i] += value;
        return accum[i];
      });
      const bottom = top.map((value, i) => value - (values[i] || 0));
      const pathTop = top
        .map((value, i) => {
          const x = xFor(i, count);
          const y = yFor(value);
          return `${x},${y}`;
        })
        .join(" ");
      const pathBottom = bottom
        .map((value, i) => {
          const x = xFor(count - 1 - i, count);
          const y = yFor(value);
          return `${x},${y}`;
        })
        .join(" ");
      const color = colors[idx % colors.length];
      svg += `<polygon fill="${color}44" stroke="${color}" stroke-width="1" points="${pathTop} ${pathBottom}" />`;
    });
  }

  svg += "</svg>";
  container.__usageChartBound = false;
  container.innerHTML = svg;
  attachUsageChartInteraction(container, {
    buckets,
    series,
    segment: usageChartState.segment,
    scaleMax,
    width,
    height,
    padding,
    chartWidth,
    chartHeight,
  });
}

function setChartLoading(container, loading) {
  if (!container) return;
  container.classList.toggle("loading", loading);
}

function attachUsageChartInteraction(container, state) {
  container.__usageChartState = state;
  if (container.__usageChartBound) return;
  container.__usageChartBound = true;

  const focus = document.createElement("div");
  focus.className = "usage-chart-focus";
  const dot = document.createElement("div");
  dot.className = "usage-chart-dot";
  const tooltip = document.createElement("div");
  tooltip.className = "usage-chart-tooltip";
  container.appendChild(focus);
  container.appendChild(dot);
  container.appendChild(tooltip);

  const updateTooltip = (event) => {
    const chartState = container.__usageChartState;
    if (!chartState) return;
    const rect = container.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const normalizedX = (x / rect.width) * chartState.width;
    const usableWidth = chartState.chartWidth;
    const localX = Math.min(
      Math.max(normalizedX - chartState.padding, 0),
      usableWidth
    );
    const index = Math.round(
      (localX / usableWidth) * (chartState.buckets.length - 1)
    );
    const clampedIndex = Math.max(
      0,
      Math.min(chartState.buckets.length - 1, index)
    );
    const xPos =
      chartState.padding +
      (clampedIndex / (chartState.buckets.length - 1 || 1)) * usableWidth;

    const totals = chartState.series.reduce((sum, entry) => {
      return sum + (entry.values?.[clampedIndex] || 0);
    }, 0);
    const yPos =
      chartState.padding +
      chartState.chartHeight -
      (totals / chartState.scaleMax) * chartState.chartHeight;

    focus.style.opacity = "1";
    dot.style.opacity = "1";
    focus.style.left = `${(xPos / chartState.width) * 100}%`;
    dot.style.left = `${(xPos / chartState.width) * 100}%`;
    dot.style.top = `${(yPos / chartState.height) * 100}%`;

    const bucketLabel = chartState.buckets[clampedIndex];
    const rows = [];
    rows.push(
      `<div class="usage-chart-tooltip-row"><span>Total</span><span>${formatTokensCompact(
        totals
      )}</span></div>`
    );

    if (chartState.segment !== "none") {
      const ranked = chartState.series
        .map((entry) => ({
          key: entry.key,
          value: entry.values?.[clampedIndex] || 0,
        }))
        .filter((entry) => entry.value > 0)
        .sort((a, b) => b.value - a.value)
        .slice(0, 4);
      ranked.forEach((entry) => {
        rows.push(
          `<div class="usage-chart-tooltip-row"><span>${entry.key}</span><span>${formatTokensCompact(
            entry.value
          )}</span></div>`
        );
      });
    }

    tooltip.innerHTML = `<div class="usage-chart-tooltip-title">${bucketLabel}</div>${rows.join(
      ""
    )}`;

    const tooltipRect = tooltip.getBoundingClientRect();
    let tooltipLeft = x + 10;
    if (tooltipLeft + tooltipRect.width > rect.width) {
      tooltipLeft = x - tooltipRect.width - 10;
    }
    tooltipLeft = Math.max(6, tooltipLeft);
    let tooltipTop = yPos / chartState.height * rect.height - tooltipRect.height - 8;
    if (tooltipTop < 6) {
      tooltipTop = yPos / chartState.height * rect.height + 8;
    }
    tooltip.style.opacity = "1";
    tooltip.style.transform = `translate(${tooltipLeft}px, ${tooltipTop}px)`;
  };

  container.addEventListener("pointermove", updateTooltip);
  container.addEventListener("pointerleave", () => {
    focus.style.opacity = "0";
    dot.style.opacity = "0";
    tooltip.style.opacity = "0";
  });
}

async function loadUsageSeries() {
  const container = document.getElementById("usage-chart-canvas");
  try {
    const data = await api(`/api/usage/series?${buildUsageSeriesQuery()}`);
    setChartLoading(container, data?.status === "loading");
    renderUsageChart(data);
    if (data?.status === "loading") {
      scheduleUsageSeriesRetry();
    } else {
      clearUsageSeriesRetry();
    }
  } catch (err) {
    setChartLoading(container, false);
    renderUsageChart(null);
    clearUsageSeriesRetry();
  }
}

function scheduleUsageSeriesRetry() {
  clearUsageSeriesRetry();
  usageSeriesRetryTimer = setTimeout(() => {
    loadUsageSeries();
  }, 1500);
}

function clearUsageSeriesRetry() {
  if (usageSeriesRetryTimer) {
    clearTimeout(usageSeriesRetryTimer);
    usageSeriesRetryTimer = null;
  }
}

async function loadUsage() {
  setUsageLoading(true);
  try {
    const data = await api("/api/usage");
    renderUsage(data);
    loadUsageSeries();
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

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Checking...";

  let check;
  try {
    check = await api("/system/update/check");
  } catch (err) {
    check = { update_available: true, message: err.message || "Unable to check for updates." };
  }

  if (!check?.update_available) {
    flash(check?.message || "No update available.");
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  const confirmed = await confirmModal(
    `${check?.message || "Update available."} Update Codex Autorunner? The service will restart.`
  );
  if (!confirmed) {
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  btn.textContent = "Updating...";

  try {
    const res = await api("/system/update", { method: "POST" });
    flash(res.message || "Update started. Reloading...", "success");
    // Disable interaction
    document.body.style.pointerEvents = "none";
    // Wait for restart (approx 5-10s) then reload
    setTimeout(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("v", String(Date.now()));
      window.location.replace(url.toString());
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

function initUsageChartControls() {
  const segmentSelect = document.getElementById("usage-chart-segment");
  const rangeSelect = document.getElementById("usage-chart-range");
  if (segmentSelect) {
    segmentSelect.value = usageChartState.segment;
    segmentSelect.addEventListener("change", () => {
      usageChartState.segment = segmentSelect.value;
      loadUsageSeries();
    });
  }
  if (rangeSelect) {
    rangeSelect.value = String(usageChartState.windowDays);
    rangeSelect.addEventListener("change", () => {
      const value = Number(rangeSelect.value);
      usageChartState.windowDays = Number.isNaN(value)
        ? usageChartState.windowDays
        : value;
      loadUsageSeries();
    });
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

function isDocsReady() {
  return document.body?.dataset?.docsReady === "true";
}

function openSummaryDoc() {
  const summaryChip = document.querySelector('.chip[data-doc="summary"]');
  if (summaryChip) summaryChip.click();
}

export function initDashboard() {
  initSettings();
  initUsageChartControls();
  subscribe("state:update", renderState);
  subscribe("docs:updated", handleDocsEvent);
  subscribe("docs:loaded", handleDocsEvent);
  subscribe("docs:ready", () => {
    if (!isDocsReady()) {
      document.body.dataset.docsReady = "true";
    }
    if (pendingSummaryOpen) {
      pendingSummaryOpen = false;
      openSummaryDoc();
    }
  });
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
  bindAction("refresh-preview", loadTodoPreview);
  // Try loading from cache first
  const cachedState = loadFromCache("state");
  if (cachedState) renderState(cachedState);

  const cachedUsage = loadFromCache("usage");
  if (cachedUsage) renderUsage(cachedUsage);

  const cachedTodo = loadFromCache("todo-doc");
  if (typeof cachedTodo === "string") {
    updateTodoPreview(cachedTodo);
  }

  const summaryBtn = document.getElementById("open-summary");
  if (summaryBtn) {
    summaryBtn.addEventListener("click", () => {
      const docsTab = document.querySelector('.tab[data-target="docs"]');
      if (docsTab) docsTab.click();
      if (isDocsReady()) {
        requestAnimationFrame(openSummaryDoc);
      } else {
        pendingSummaryOpen = true;
      }
    });
  }

  // Initial load
  loadUsage();
  loadTodoPreview();
  loadVersion();
  checkUpdateStatus();
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

async function loadVersion() {
  const versionEl = document.getElementById("repo-version");
  if (!versionEl) return;
  try {
    const data = await api("/api/version", { method: "GET" });
    const version = data?.asset_version || "";
    versionEl.textContent = version ? `v${version}` : "v–";
  } catch (_err) {
    versionEl.textContent = "v–";
  }
}

async function checkUpdateStatus() {
  try {
    const data = await api("/system/update/status", { method: "GET" });
    if (!data || !data.status) return;
    const stamp = data.at ? String(data.at) : "";
    if (stamp && sessionStorage.getItem(UPDATE_STATUS_SEEN_KEY) === stamp) return;
    if (data.status === "rollback" || data.status === "error") {
      flash(data.message || "Update failed; rollback attempted.", "error");
    }
    if (stamp) sessionStorage.setItem(UPDATE_STATUS_SEEN_KEY, stamp);
  } catch (_err) {
    // ignore
  }
}
