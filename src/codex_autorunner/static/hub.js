import {
  api,
  flash,
  statusPill,
  resolvePath,
  confirmModal,
  inputModal,
} from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { HUB_BASE } from "./env.js";

let hubData = { repos: [], last_scan_at: null };
const repoPrCache = new Map();
const repoPrFetches = new Set();
const prefetchedUrls = new Set();

const HUB_CACHE_TTL_MS = 30000;
const HUB_CACHE_KEY = `car:hub:${HUB_BASE || "/"}`;
const HUB_USAGE_CACHE_KEY = `car:hub-usage:${HUB_BASE || "/"}`;
const PR_CACHE_TTL_MS = 120000;

const repoListEl = document.getElementById("hub-repo-list");
const lastScanEl = document.getElementById("hub-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");
const hubUsageList = document.getElementById("hub-usage-list");
const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
const hubUsageChartCanvas = document.getElementById("hub-usage-chart-canvas");
const hubUsageChartRange = document.getElementById("hub-usage-chart-range");
const hubUsageChartSegment = document.getElementById("hub-usage-chart-segment");
const hubVersionEl = document.getElementById("hub-version");
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";

const hubUsageChartState = {
  segment: "none",
  bucket: "day",
  windowDays: 30,
};
let hubUsageSeriesRetryTimer = null;

function saveSessionCache(key, value) {
  try {
    const payload = { at: Date.now(), value };
    sessionStorage.setItem(key, JSON.stringify(payload));
  } catch (_err) {
    // ignore session storage issues
  }
}

function loadSessionCache(key, maxAgeMs) {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const payload = JSON.parse(raw);
    if (!payload || typeof payload.at !== "number") return null;
    if (maxAgeMs && Date.now() - payload.at > maxAgeMs) return null;
    return payload.value;
  } catch (_err) {
    return null;
  }
}

function formatRunSummary(repo) {
  if (!repo.initialized) return "Not initialized";
  if (!repo.exists_on_disk) return "Missing on disk";
  if (!repo.last_run_id) return "No runs yet";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  const exit =
    repo.last_exit_code === null || repo.last_exit_code === undefined
      ? ""
      : ` exit:${repo.last_exit_code}`;
  return `#${repo.last_run_id}${exit}`;
}

function formatLastActivity(repo) {
  if (!repo.initialized) return "";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  if (!time) return "";
  return formatTimeCompact(time);
}

function setButtonLoading(scanning) {
  const buttons = [
    document.getElementById("hub-scan"),
    document.getElementById("hub-quick-scan"),
    document.getElementById("hub-refresh"),
  ];
  buttons.forEach((btn) => {
    if (!btn) return;
    btn.disabled = scanning;
    if (scanning) {
      btn.classList.add("loading");
    } else {
      btn.classList.remove("loading");
    }
  });
}

function formatTimeCompact(isoString) {
  if (!isoString) return "–";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return date.toLocaleDateString();
}

function renderSummary(repos) {
  const running = repos.filter((r) => r.status === "running").length;
  const missing = repos.filter((r) => !r.exists_on_disk).length;
  if (totalEl) totalEl.textContent = repos.length.toString();
  if (runningEl) runningEl.textContent = running.toString();
  if (missingEl) missingEl.textContent = missing.toString();
  if (lastScanEl) {
    lastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
  }
}

function formatTokensCompact(val) {
  if (val === null || val === undefined) return "0";
  const num = Number(val);
  if (Number.isNaN(num)) return val;
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(0)}k`;
  return num.toLocaleString();
}

function formatTokensAxis(val) {
  const num = Number(val);
  if (Number.isNaN(num)) return "0";
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return Math.round(num).toString();
}

function renderHubUsage(data) {
  if (!hubUsageList) return;
  if (hubUsageMeta) {
    hubUsageMeta.textContent = data?.codex_home || "–";
  }
  if (!data || !data.repos) {
    hubUsageList.innerHTML =
      '<span class="muted small">Usage unavailable</span>';
    return;
  }
  if (!data.repos.length && (!data.unmatched || !data.unmatched.events)) {
    hubUsageList.innerHTML = '<span class="muted small">No token events</span>';
    return;
  }
  hubUsageList.innerHTML = "";
  const entries = [...data.repos].sort(
    (a, b) => (b.totals?.total_tokens || 0) - (a.totals?.total_tokens || 0)
  );
  entries.forEach((repo) => {
    const div = document.createElement("div");
    div.className = "hub-usage-chip";
    const totals = repo.totals || {};
    const cached = totals.cached_input_tokens || 0;
    const cachePercent = totals.input_tokens
      ? Math.round((cached / totals.input_tokens) * 100)
      : 0;
    div.innerHTML = `
      <span class="hub-usage-chip-name">${repo.id}</span>
      <span class="hub-usage-chip-total">${formatTokensCompact(
        totals.total_tokens
      )}</span>
      <span class="hub-usage-chip-meta">${
        repo.events ?? 0
      }ev · ${cachePercent}%↻</span>
    `;
    hubUsageList.appendChild(div);
  });
  if (data.unmatched && data.unmatched.events) {
    const div = document.createElement("div");
    div.className = "hub-usage-chip hub-usage-chip-unmatched";
    const totals = data.unmatched.totals || {};
    div.innerHTML = `
      <span class="hub-usage-chip-name">other</span>
      <span class="hub-usage-chip-total">${formatTokensCompact(
        totals.total_tokens
      )}</span>
      <span class="hub-usage-chip-meta">${data.unmatched.events}ev</span>
    `;
    hubUsageList.appendChild(div);
  }
}

async function loadHubUsage() {
  if (hubUsageRefresh) hubUsageRefresh.disabled = true;
  try {
    const data = await api("/hub/usage");
    renderHubUsage(data);
    loadHubUsageSeries();
    saveSessionCache(HUB_USAGE_CACHE_KEY, data);
  } catch (err) {
    flash(err.message || "Failed to load usage", "error");
    renderHubUsage(null);
  } finally {
    if (hubUsageRefresh) hubUsageRefresh.disabled = false;
  }
}

function buildHubUsageSeriesQuery() {
  const params = new URLSearchParams();
  const now = new Date();
  const since = new Date(now.getTime() - hubUsageChartState.windowDays * 86400000);
  const bucket = hubUsageChartState.windowDays >= 180 ? "week" : "day";
  params.set("since", since.toISOString());
  params.set("until", now.toISOString());
  params.set("bucket", bucket);
  params.set("segment", hubUsageChartState.segment);
  return params.toString();
}

function renderHubUsageChart(data) {
  if (!hubUsageChartCanvas) return;
  const buckets = data?.buckets || [];
  const series = data?.series || [];
  const isLoading = data?.status === "loading";
  if (!buckets.length || !series.length) {
    hubUsageChartCanvas.__usageChartBound = false;
    hubUsageChartCanvas.innerHTML = isLoading
      ? '<div class="usage-chart-empty">Loading…</div>'
      : '<div class="usage-chart-empty">No data</div>';
    return;
  }

  const { width, height } = getChartSize(hubUsageChartCanvas, 560, 160);
  const padding = 14;
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
    "#c18bff",
    "#f5d36c",
  ];

  const { series: displaySeries } = limitSeries(series, 6, "rest");

  let scaleMax = 1;
  if (hubUsageChartState.segment === "none") {
    const values = displaySeries[0]?.values || [];
    scaleMax = Math.max(...values, 1);
  } else {
    const totals = new Array(buckets.length).fill(0);
    displaySeries.forEach((entry) => {
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

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Hub usage trend">`;
  svg += `
    <defs>
      <linearGradient id="hub-usage-line-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#6cf5d8" stop-opacity="0.35" />
        <stop offset="100%" stop-color="#6cf5d8" stop-opacity="0" />
      </linearGradient>
      <filter id="hub-usage-line-glow" x="-20%" y="-20%" width="140%" height="140%">
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
  svg += `<text x="${padding}" y="${padding + 12}" fill="rgba(203, 213, 225, 0.7)" font-size="9">${maxLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight / 2 + 4
  }" fill="rgba(203, 213, 225, 0.6)" font-size="9">${midLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight + 2
  }" fill="rgba(203, 213, 225, 0.5)" font-size="9">0</text>`;

  if (hubUsageChartState.segment === "none") {
    const values = displaySeries[0]?.values || [];
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
      svg += `<path d="${areaPath}" fill="url(#hub-usage-line-fill)" />`;
      svg += `<path d="${linePath}" fill="none" stroke="#6cf5d8" stroke-width="2" filter="url(#hub-usage-line-glow)" />`;
      svg += `<circle cx="${x0}" cy="${y0}" r="3" fill="#6cf5d8" />`;
    }
  } else {
    const count = buckets.length;
    const accum = new Array(count).fill(0);
    displaySeries.forEach((entry, idx) => {
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
  hubUsageChartCanvas.__usageChartBound = false;
  hubUsageChartCanvas.innerHTML = svg;
  attachHubUsageChartInteraction(hubUsageChartCanvas, {
    buckets,
    series: displaySeries,
    segment: hubUsageChartState.segment,
    scaleMax,
    width,
    height,
    padding,
    chartWidth,
    chartHeight,
  });
}

function getChartSize(container, fallbackWidth, fallbackHeight) {
  const rect = container.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width || fallbackWidth));
  const height = Math.max(1, Math.round(rect.height || fallbackHeight));
  return { width, height };
}

function limitSeries(series, maxSeries, restKey) {
  if (series.length <= maxSeries) return { series };
  const sorted = [...series].sort((a, b) => (b.total || 0) - (a.total || 0));
  const top = sorted.slice(0, maxSeries);
  const rest = sorted.slice(maxSeries);
  if (!rest.length) return { series: top };
  const values = new Array((top[0]?.values || []).length).fill(0);
  rest.forEach((entry) => {
    (entry.values || []).forEach((value, i) => {
      values[i] += value;
    });
  });
  const total = values.reduce((sum, value) => sum + value, 0);
  top.push({ key: restKey, repo: null, token_type: null, total, values });
  return { series: top };
}

function attachHubUsageChartInteraction(container, state) {
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
        .slice(0, 6);
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
    let tooltipLeft = x + 12;
    if (tooltipLeft + tooltipRect.width > rect.width) {
      tooltipLeft = x - tooltipRect.width - 12;
    }
    tooltipLeft = Math.max(6, tooltipLeft);
    let tooltipTop = (yPos / chartState.height) * rect.height - tooltipRect.height - 10;
    if (tooltipTop < 6) {
      tooltipTop = (yPos / chartState.height) * rect.height + 10;
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

async function loadHubUsageSeries() {
  if (!hubUsageChartCanvas) return;
  try {
    const data = await api(`/hub/usage/series?${buildHubUsageSeriesQuery()}`);
    hubUsageChartCanvas.classList.toggle("loading", data?.status === "loading");
    renderHubUsageChart(data);
    if (data?.status === "loading") {
      scheduleHubUsageSeriesRetry();
    } else {
      clearHubUsageSeriesRetry();
    }
  } catch (_err) {
    hubUsageChartCanvas.classList.remove("loading");
    renderHubUsageChart(null);
    clearHubUsageSeriesRetry();
  }
}

function scheduleHubUsageSeriesRetry() {
  clearHubUsageSeriesRetry();
  hubUsageSeriesRetryTimer = setTimeout(() => {
    loadHubUsageSeries();
  }, 1500);
}

function clearHubUsageSeriesRetry() {
  if (hubUsageSeriesRetryTimer) {
    clearTimeout(hubUsageSeriesRetryTimer);
    hubUsageSeriesRetryTimer = null;
  }
}

function initHubUsageChartControls() {
  if (hubUsageChartRange) {
    hubUsageChartRange.value = String(hubUsageChartState.windowDays);
    hubUsageChartRange.addEventListener("change", () => {
      const value = Number(hubUsageChartRange.value);
      hubUsageChartState.windowDays = Number.isNaN(value)
        ? hubUsageChartState.windowDays
        : value;
      loadHubUsageSeries();
    });
  }
  if (hubUsageChartSegment) {
    hubUsageChartSegment.value = hubUsageChartState.segment;
    hubUsageChartSegment.addEventListener("change", () => {
      hubUsageChartState.segment = hubUsageChartSegment.value;
      loadHubUsageSeries();
    });
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

function initHubSettings() {
  const settingsBtn = document.getElementById("hub-settings");
  const modal = document.getElementById("hub-settings-modal");
  const closeBtn = document.getElementById("hub-settings-close");
  const updateBtn = document.getElementById("hub-update-btn");

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
      handleSystemUpdate("hub-update-btn")
    );
  }
}

function buildActions(repo) {
  const actions = [];
  const missing = !repo.exists_on_disk;
  const kind = repo.kind || "base";
  if (!missing && (repo.init_error || repo.mount_error)) {
    actions.push({
      key: "init",
      label: repo.initialized ? "Re-init" : "Init",
      kind: "primary",
    });
  } else if (!missing && !repo.initialized) {
    actions.push({ key: "init", label: "Init", kind: "primary" });
  }
  if (!missing && kind === "base") {
    actions.push({ key: "new_worktree", label: "New Worktree", kind: "ghost" });
  }
  if (!missing && kind === "worktree") {
    actions.push({
      key: "cleanup_worktree",
      label: "Cleanup",
      kind: "ghost",
      title: "Remove worktree and delete branch",
    });
  }
  return actions;
}

function inferBaseId(repo) {
  if (!repo) return null;
  if (repo.worktree_of) return repo.worktree_of;
  if (typeof repo.id === "string" && repo.id.includes("--")) {
    return repo.id.split("--", 1)[0];
  }
  return null;
}

function renderRepos(repos) {
  if (!repoListEl) return;
  repoListEl.innerHTML = "";
  if (!repos.length) {
    repoListEl.innerHTML =
      '<div class="hub-empty muted">No repos discovered yet. Run a scan or create a new repo.</div>';
    return;
  }

  const bases = repos.filter((r) => (r.kind || "base") === "base");
  const worktrees = repos.filter((r) => (r.kind || "base") === "worktree");
  const byBase = new Map();
  bases.forEach((b) => byBase.set(b.id, { base: b, worktrees: [] }));
  const orphanWorktrees = [];
  worktrees.forEach((w) => {
    const baseId = inferBaseId(w);
    if (baseId && byBase.has(baseId)) {
      byBase.get(baseId).worktrees.push(w);
    } else {
      orphanWorktrees.push(w);
    }
  });

  const orderedGroups = [...byBase.values()].sort((a, b) =>
    String(a.base?.id || "").localeCompare(String(b.base?.id || ""))
  );

  const renderRepoCard = (repo, { isWorktreeRow = false } = {}) => {
    const card = document.createElement("div");
    card.className = isWorktreeRow
      ? "hub-repo-card hub-worktree-card"
      : "hub-repo-card";
    card.dataset.repoId = repo.id;

    // Make card clickable only for repos that are actually mounted
    const canNavigate = repo.mounted === true;
    if (canNavigate) {
      card.classList.add("hub-repo-clickable");
      card.dataset.href = resolvePath(`/repos/${repo.id}/`);
      card.setAttribute("role", "link");
      card.setAttribute("tabindex", "0");
    }

    const actions = buildActions(repo)
      .map(
        (action) =>
          `<button class="${action.kind} sm" data-action="${
            action.key
          }" data-repo="${repo.id}"${
            action.title ? ` title="${action.title}"` : ""
          }>${action.label}</button>`
      )
      .join("");

    const lockBadge =
      repo.lock_status && repo.lock_status !== "unlocked"
        ? `<span class="pill pill-small pill-warn">${repo.lock_status.replace(
            "_",
            " "
          )}</span>`
        : "";
    const initBadge = !repo.initialized
      ? '<span class="pill pill-small pill-warn">uninit</span>'
      : "";

    // Build note for errors
    let noteText = "";
    if (!repo.exists_on_disk) {
      noteText = "Missing on disk";
    } else if (repo.init_error) {
      noteText = repo.init_error;
    } else if (repo.mount_error) {
      noteText = `Cannot open: ${repo.mount_error}`;
    }
    const note = noteText ? `<div class="hub-repo-note">${noteText}</div>` : "";

    // Show open indicator only for navigable repos
    const openIndicator = canNavigate
      ? '<span class="hub-repo-open-indicator">→</span>'
      : "";

    // Build compact info line
    const runSummary = formatRunSummary(repo);
    const lastActivity = formatLastActivity(repo);
    const infoItems = [];
    if (
      runSummary &&
      runSummary !== "No runs yet" &&
      runSummary !== "Not initialized"
    ) {
      infoItems.push(runSummary);
    }
    if (lastActivity) {
      infoItems.push(lastActivity);
    }
    const infoLine =
      infoItems.length > 0
        ? `<span class="hub-repo-info-line">${infoItems.join(" · ")}</span>`
        : "";

    // Best-effort PR pill for mounted repos (does not block rendering).
    const prInfo = repoPrCache.get(repo.id)?.data;
    const prPill = prInfo?.links?.files
      ? `<a class="pill pill-small hub-pr-pill" href="${
          prInfo.links.files
        }" target="_blank" rel="noopener noreferrer" title="${
          prInfo.pr?.title || "Open PR files"
        }">PR${prInfo.pr?.number ? ` #${prInfo.pr.number}` : ""}</a>`
      : "";

    card.innerHTML = `
      <div class="hub-repo-row">
        <div class="hub-repo-left">
            <span class="pill pill-small hub-status-pill">${repo.status}</span>
            ${lockBadge}
            ${initBadge}
          </div>
        <div class="hub-repo-center">
          <span class="hub-repo-title">${repo.display_name}</span>
          <div class="hub-repo-subline">
            ${infoLine}
            ${prPill}
          </div>
        </div>
        <div class="hub-repo-right">
          ${actions || ""}
          ${openIndicator}
        </div>
      </div>
      ${note}
    `;

    const statusEl = card.querySelector(".hub-status-pill");
    if (statusEl) {
      statusPill(statusEl, repo.status);
    }

    repoListEl.appendChild(card);
  };

  orderedGroups.forEach((group) => {
    const repo = group.base;
    renderRepoCard(repo, { isWorktreeRow: false });
    if (group.worktrees && group.worktrees.length) {
      const list = document.createElement("div");
      list.className = "hub-worktree-list";
      group.worktrees
        .sort((a, b) => String(a.id).localeCompare(String(b.id)))
        .forEach((wt) => {
          const row = document.createElement("div");
          row.className = "hub-worktree-row";
          // render as mini-card via innerHTML generated by renderRepoCard logic:
          // easiest: reuse renderRepoCard but with separate container
          const tmp = document.createElement("div");
          tmp.className = "hub-worktree-row-inner";
          list.appendChild(tmp);
          // Temporarily render into tmp by calling renderRepoCard and moving the node.
          const beforeCount = repoListEl.children.length;
          renderRepoCard(wt, { isWorktreeRow: true });
          const newNode = repoListEl.children[beforeCount];
          if (newNode) {
            repoListEl.removeChild(newNode);
            tmp.appendChild(newNode);
          }
        });
      repoListEl.appendChild(list);
    }
  });

  if (orphanWorktrees.length) {
    const header = document.createElement("div");
    header.className = "hub-worktree-orphans muted small";
    header.textContent = "Orphan worktrees";
    repoListEl.appendChild(header);
    orphanWorktrees
      .sort((a, b) => String(a.id).localeCompare(String(b.id)))
      .forEach((wt) => renderRepoCard(wt, { isWorktreeRow: true }));
  }
}

async function refreshRepoPrCache(repos) {
  const mounted = (repos || []).filter((r) => r && r.mounted === true);
  if (!mounted.length) return;
  const tasks = mounted.map(async (repo) => {
    const cached = repoPrCache.get(repo.id);
    if (
      cached &&
      typeof cached.fetchedAt === "number" &&
      Date.now() - cached.fetchedAt < PR_CACHE_TTL_MS
    ) {
      return;
    }
    if (repoPrFetches.has(repo.id)) return;
    repoPrFetches.add(repo.id);
    try {
      const pr = await api(`/repos/${repo.id}/api/github/pr`, {
        method: "GET",
      });
      repoPrCache.set(repo.id, { data: pr, fetchedAt: Date.now() });
    } catch (err) {
      // Best-effort: ignore GitHub errors so hub stays fast.
    } finally {
      repoPrFetches.delete(repo.id);
    }
  });
  await Promise.allSettled(tasks);
  // Re-render to show pills without blocking initial load.
  renderRepos(hubData.repos || []);
}

async function refreshHub({ scan = false } = {}) {
  setButtonLoading(true);
  try {
    const path = scan ? "/hub/repos/scan" : "/hub/repos";
    const data = await api(path, { method: scan ? "POST" : "GET" });
    hubData = data;
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(data.repos || []);
    renderRepos(data.repos || []);
    refreshRepoPrCache(data.repos || []).catch(() => {});
    await loadHubUsage();
  } catch (err) {
    flash(err.message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

async function createRepo(repoId, repoPath, gitInit) {
  try {
    const payload = { id: repoId };
    if (repoPath) payload.path = repoPath;
    payload.git_init = gitInit;
    await api("/hub/repos", { method: "POST", body: payload });
    flash(`Created repo: ${repoId}`);
    await refreshHub();
    return true;
  } catch (err) {
    flash(err.message || "Failed to create repo", "error");
    return false;
  }
}

function showCreateRepoModal() {
  const modal = document.getElementById("create-repo-modal");
  if (modal) {
    modal.hidden = false;
    const input = document.getElementById("create-repo-id");
    if (input) {
      input.value = "";
      input.focus();
    }
    const pathInput = document.getElementById("create-repo-path");
    if (pathInput) pathInput.value = "";
    const gitCheck = document.getElementById("create-repo-git");
    if (gitCheck) gitCheck.checked = true;
  }
}

function hideCreateRepoModal() {
  const modal = document.getElementById("create-repo-modal");
  if (modal) modal.hidden = true;
}

async function handleCreateRepoSubmit() {
  const idInput = document.getElementById("create-repo-id");
  const pathInput = document.getElementById("create-repo-path");
  const gitCheck = document.getElementById("create-repo-git");

  const repoId = idInput?.value?.trim();
  const repoPath = pathInput?.value?.trim() || null;
  const gitInit = gitCheck?.checked ?? true;

  if (!repoId) {
    flash("Repo ID is required", "error");
    return;
  }

  const ok = await createRepo(repoId, repoPath, gitInit);
  if (ok) {
    hideCreateRepoModal();
  }
}

async function handleRepoAction(repoId, action) {
  const buttons = repoListEl?.querySelectorAll(
    `button[data-repo="${repoId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => (btn.disabled = true));
  try {
    const pathMap = {
      init: `/hub/repos/${repoId}/init`,
    };
    if (action === "new_worktree") {
      const branch = await inputModal("New worktree branch name:", {
        placeholder: "feature/my-branch",
        confirmText: "Create",
      });
      if (!branch) return;
      const created = await api("/hub/worktrees/create", {
        method: "POST",
        body: { base_repo_id: repoId, branch },
      });
      flash(`Created worktree: ${created.id}`);
      await refreshHub();
      if (created?.mounted) {
        window.location.href = resolvePath(`/repos/${created.id}/`);
      }
      return;
    }
    if (action === "cleanup_worktree") {
      // Extract display name for clearer messaging
      const displayName = repoId.includes("--")
        ? repoId.split("--").pop()
        : repoId;
      const ok = await confirmModal(
        `Remove worktree "${displayName}"? This will delete the worktree directory and its branch.`,
        { confirmText: "Remove", danger: true }
      );
      if (!ok) return;
      await api("/hub/worktrees/cleanup", {
        method: "POST",
        body: { worktree_repo_id: repoId },
      });
      flash(`Removed worktree: ${repoId}`);
      await refreshHub();
      return;
    }

    const path = pathMap[action];
    if (!path) return;
    await api(path, { method: "POST" });
    flash(`${action} sent to ${repoId}`);
    await refreshHub();
  } catch (err) {
    flash(err.message || "Hub action failed", "error");
  } finally {
    buttons?.forEach((btn) => (btn.disabled = false));
  }
}

function attachHubHandlers() {
  initHubSettings();
  const scanBtn = document.getElementById("hub-scan");
  const refreshBtn = document.getElementById("hub-refresh");
  const quickScanBtn = document.getElementById("hub-quick-scan");
  const newRepoBtn = document.getElementById("hub-new-repo");
  const createCancelBtn = document.getElementById("create-repo-cancel");
  const createSubmitBtn = document.getElementById("create-repo-submit");
  const createRepoId = document.getElementById("create-repo-id");

  scanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  quickScanBtn?.addEventListener("click", () => refreshHub({ scan: true }));
  refreshBtn?.addEventListener("click", () => refreshHub({ scan: false }));
  hubUsageRefresh?.addEventListener("click", () => loadHubUsage());

  newRepoBtn?.addEventListener("click", showCreateRepoModal);
  createCancelBtn?.addEventListener("click", hideCreateRepoModal);
  createSubmitBtn?.addEventListener("click", handleCreateRepoSubmit);

  // Allow Enter key in the repo ID input to submit
  createRepoId?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleCreateRepoSubmit();
    }
  });

  // Close modal on Escape key
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideCreateRepoModal();
    }
  });

  // Close modal when clicking overlay background
  const createRepoModal = document.getElementById("create-repo-modal");
  createRepoModal?.addEventListener("click", (e) => {
    if (e.target === createRepoModal) {
      hideCreateRepoModal();
    }
  });

  repoListEl?.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;

    // Allow PR pill navigation without triggering card navigation.
    const prLink = target.closest("a.hub-pr-pill");
    if (prLink) {
      event.stopPropagation();
      return;
    }

    // Handle action buttons - stop propagation to prevent card navigation
    const btn = target.closest("button[data-action]");
    if (btn) {
      event.stopPropagation();
      const action = btn.dataset.action;
      const repoId = btn.dataset.repo;
      if (action && repoId) {
        handleRepoAction(repoId, action);
      }
      return;
    }

    // Handle card click for navigation
    const card = target.closest(".hub-repo-clickable");
    if (card && card.dataset.href) {
      window.location.href = card.dataset.href;
    }
  });

  // Support keyboard navigation for cards
  repoListEl?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target.classList.contains("hub-repo-clickable")
      ) {
        event.preventDefault();
        if (target.dataset.href) {
          window.location.href = target.dataset.href;
        }
      }
    }
  });

  repoListEl?.addEventListener("mouseover", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const card = target.closest(".hub-repo-clickable");
    if (card && card.dataset.href) {
      prefetchRepo(card.dataset.href);
    }
  });

  repoListEl?.addEventListener("pointerdown", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const card = target.closest(".hub-repo-clickable");
    if (card && card.dataset.href) {
      prefetchRepo(card.dataset.href);
    }
  });
}

/**
 * Silent refresh for auto-refresh - doesn't show loading state on buttons.
 */
async function silentRefreshHub() {
  try {
    const data = await api("/hub/repos", { method: "GET" });
    hubData = data;
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(data.repos || []);
    renderRepos(data.repos || []);
    // Also refresh usage silently
    try {
      const usageData = await api("/hub/usage");
      renderHubUsage(usageData);
      saveSessionCache(HUB_USAGE_CACHE_KEY, usageData);
    } catch (err) {
      // Silently ignore usage errors
    }
  } catch (err) {
    // Silently fail for background refresh
    console.error("Auto-refresh hub failed:", err);
  }
}

async function loadHubVersion() {
  if (!hubVersionEl) return;
  try {
    const data = await api("/hub/version", { method: "GET" });
    const version = data?.asset_version || "";
    hubVersionEl.textContent = version ? `v${version}` : "v–";
  } catch (_err) {
    hubVersionEl.textContent = "v–";
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

function prefetchRepo(url) {
  if (!url || prefetchedUrls.has(url)) return;
  prefetchedUrls.add(url);
  fetch(url, { method: "GET", headers: { "x-prefetch": "1" } }).catch(() => {});
}

export function initHub() {
  if (!repoListEl) return;
  attachHubHandlers();
  initHubUsageChartControls();
  const cachedHub = loadSessionCache(HUB_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedHub) {
    hubData = cachedHub;
    renderSummary(cachedHub.repos || []);
    renderRepos(cachedHub.repos || []);
  }
  const cachedUsage = loadSessionCache(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedUsage) {
    renderHubUsage(cachedUsage);
  }
  loadHubUsageSeries();
  refreshHub();
  loadHubVersion();
  checkUpdateStatus();

  // Register auto-refresh for hub repo list
  // Hub is a top-level page so we use tabId: null (global)
  registerAutoRefresh("hub-repos", {
    callback: silentRefreshHub,
    tabId: null, // Hub is the main page, not a tab
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false, // Already called refreshHub() above
  });
}
