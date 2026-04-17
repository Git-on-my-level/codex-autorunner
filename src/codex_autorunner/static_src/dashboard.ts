import { api, flash, statusPill } from "./utils.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { preserveScroll } from "./preserve.js";
import { createSmartRefresh, type SmartRefreshReason } from "./smartRefresh.js";
import {
  type UsageChartData,
  formatTokensCompact,
  usageSeriesSignature,
  renderUsageChart,
  setChartLoading,
} from "./usageChart.js";

const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
const ANALYTICS_SUMMARY_CACHE_KEY = "analytics-summary";

interface UsageChartState {
  segment: string;
  bucket: string;
  windowDays: number;
}

const usageChartState: UsageChartState = {
  segment: "none",
  bucket: "day",
  windowDays: 30,
};

let usageSeriesRetryTimer: ReturnType<typeof setTimeout> | null = null;
let usageSummaryRetryTimer: ReturnType<typeof setTimeout> | null = null;

const DASHBOARD_REFRESH_REASONS: SmartRefreshReason[] = ["initial", "background", "manual"];

function normalizeRefreshReason(reason?: SmartRefreshReason): SmartRefreshReason {
  if (reason && DASHBOARD_REFRESH_REASONS.includes(reason)) return reason;
  return "manual";
}

function mapAutoRefreshReason(ctx: RefreshContext): SmartRefreshReason {
  if (ctx.reason === "manual") return "manual";
  return "background";
}

function setUsageLoading(loading: boolean): void {
  const btn = document.getElementById("usage-refresh");
  if (!btn) return;
  (btn as HTMLButtonElement).disabled = loading;
  btn.classList.toggle("loading", loading);
}



function renderUsageProgressBar(container: HTMLElement | null, percent: number | null, windowMinutes: number | null | undefined): void {
  if (!container) return;
  
  const pct = typeof percent === "number" ? Math.min(100, Math.max(0, percent)) : 0;
  const hasData = typeof percent === "number";
  
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

interface UsageTotals {
  total_tokens?: number | null;
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_output_tokens?: number | null;
}

interface RateLimit {
  used_percent?: number | null;
  window_minutes?: number | null;
}

interface UsageData {
  totals?: UsageTotals;
  events?: number;
  latest_rate_limits?: {
    primary?: RateLimit;
    secondary?: RateLimit;
  };
  codex_home?: string | null;
  status?: string;
}

function usageSummarySignature(data: UsageData | null): string {
  if (!data) return "none";
  const totals = data.totals || {};
  const primary = data.latest_rate_limits?.primary || {};
  const secondary = data.latest_rate_limits?.secondary || {};
  return [
    data.status || "",
    data.events ?? "",
    data.codex_home ?? "",
    totals.total_tokens ?? "",
    totals.input_tokens ?? "",
    totals.cached_input_tokens ?? "",
    totals.output_tokens ?? "",
    totals.reasoning_output_tokens ?? "",
    primary.used_percent ?? "",
    primary.window_minutes ?? "",
    secondary.used_percent ?? "",
    secondary.window_minutes ?? "",
  ].join("|");
}

function renderUsage(data: UsageData | null): void {
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

  if (rate) {
    const primary = rate.primary || {};
    const secondary = rate.secondary || {};
    
    renderUsageProgressBar(primaryBarEl, primary.used_percent ?? null, primary.window_minutes);
    renderUsageProgressBar(secondaryBarEl, secondary.used_percent ?? null, secondary.window_minutes);
    
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

interface AnalyticsRunSummary {
  id: string | null;
  short_id?: string | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  current_step: string | null;
  created_at?: string | null;
}

interface AnalyticsSummary {
  run: AnalyticsRunSummary;
  failure?: Record<string, unknown> | null;
  failure_summary?: string | null;
  tickets: {
    todo_count: number;
    done_count: number;
    total_count: number;
    current_ticket: string | null;
  };
  turns: {
    total: number | null;
    current_ticket: number | null;
    dispatches: number;
    replies: number;
    diff_stats?: {
      insertions: number;
      deletions: number;
      files_changed: number;
    } | null;
  };
  agent: {
    id: string | null;
    model: string | null;
  };
}

function analyticsSummarySignature(data: AnalyticsSummary | null): string {
  if (!data) return "none";
  const run = data.run;
  const tickets = data.tickets;
  const turns = data.turns;
  const agent = data.agent;
  return [
    run?.id ?? "",
    run?.short_id ?? "",
    run?.status ?? "",
    run?.started_at ?? "",
    run?.finished_at ?? "",
    run?.duration_seconds ?? "",
    run?.current_step ?? "",
    data?.failure_summary ?? "",
    tickets?.todo_count ?? "",
    tickets?.done_count ?? "",
    tickets?.total_count ?? "",
    tickets?.current_ticket ?? "",
    turns?.total ?? "",
    turns?.current_ticket ?? "",
    turns?.dispatches ?? "",
    turns?.replies ?? "",
    turns?.diff_stats?.insertions ?? "",
    turns?.diff_stats?.deletions ?? "",
    agent?.id ?? "",
    agent?.model ?? "",
  ].join("|");
}

interface FlowRun {
  id: string;
  flow_type: string;
  status: string;
  current_step: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  state?: Record<string, unknown>;
}

const usageSummaryRefresh = createSmartRefresh<UsageData | null>({
  getSignature: (payload) => usageSummarySignature(payload),
  render: (payload) => {
    renderUsage(payload);
  },
});

const analyticsSummaryRefresh = createSmartRefresh<AnalyticsSummary | null>({
  getSignature: (payload) => analyticsSummarySignature(payload),
  render: (payload) => {
    renderTicketAnalytics(payload);
  },
});

function runHistorySignature(runs: FlowRun[]): string {
  if (!runs.length) return "none";
  return runs
    .map((run) => [
      run.id,
      run.status || "",
      run.current_step || "",
      run.created_at || "",
      run.started_at || "",
      run.finished_at || "",
    ].join(":"))
    .join("|");
}

const runHistoryRefresh = createSmartRefresh<FlowRun[]>({
  getSignature: (payload) => runHistorySignature(payload),
  render: (payload) => {
    renderRunHistory(payload);
  },
});

async function fetchTicketAnalytics(): Promise<AnalyticsSummary | null> {
  try {
    const data = (await api("/api/analytics/summary")) as AnalyticsSummary;
    saveToCache(ANALYTICS_SUMMARY_CACHE_KEY, data);
    return data;
  } catch (err) {
    const cached = loadFromCache(ANALYTICS_SUMMARY_CACHE_KEY) as AnalyticsSummary | null;
    flash((err as Error).message || "Failed to load analytics", "error");
    if (cached) return cached;
    return null;
  }
}

async function loadTicketAnalytics(reason: SmartRefreshReason = "manual"): Promise<void> {
  const resolvedReason = normalizeRefreshReason(reason);
  try {
    await analyticsSummaryRefresh.refresh(fetchTicketAnalytics, { reason: resolvedReason });
  } catch (err) {
    flash((err as Error).message || "Failed to load analytics", "error");
  }
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "–";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = seconds / 60;
  if (mins < 60) return `${Math.round(mins)}m`;
  const hours = mins / 60;
  return `${hours.toFixed(1)}h`;
}

function renderTicketAnalytics(data: AnalyticsSummary | null): void {
  const run = data?.run;
  const tickets = data?.tickets;
  const turns = data?.turns;
  const agent = data?.agent;
  const failureSummary = data?.failure_summary || "";

  const statusEl = document.getElementById("runner-status");
  if (statusEl && run) {
    statusPill(statusEl, run.status || "idle");
    statusEl.textContent = run.status || "idle";
  }

  const lastStart = document.getElementById("last-start");
  const lastFinish = document.getElementById("last-finish");
  const lastDuration = document.getElementById("last-duration");
  const todoCount = document.getElementById("todo-count");
  const doneCount = document.getElementById("done-count");
  const ticketActive = document.getElementById("ticket-active");
  const ticketTurns = document.getElementById("ticket-turns");
  const totalTurns = document.getElementById("total-turns");
  const dispatchesEl = document.getElementById("message-dispatches");
  const repliesEl = document.getElementById("message-replies");
  const runIdEl = document.getElementById("last-run-id");
  const failureEl = document.getElementById("failure-summary");

  if (lastStart) lastStart.textContent = formatIso(run?.started_at || null);
  if (lastFinish) lastFinish.textContent = formatIso(run?.finished_at || null);
  if (lastDuration) lastDuration.textContent = formatDuration(run?.duration_seconds ?? null);
  if (todoCount) todoCount.textContent = tickets ? String(tickets.todo_count) : "–";
  if (doneCount) doneCount.textContent = tickets ? String(tickets.done_count) : "–";
  if (ticketActive) {
    const ticket = tickets?.current_ticket || null;
    ticketActive.textContent = ticket ? ticket.split("/").pop() || ticket : "–";
  }
  if (ticketTurns) ticketTurns.textContent = turns?.current_ticket != null ? String(turns.current_ticket) : "–";
  if (totalTurns) totalTurns.textContent = turns?.total != null ? String(turns.total) : "–";
  const dispatchCount = turns?.dispatches ?? 0;
  if (dispatchesEl) dispatchesEl.textContent = String(dispatchCount);
  if (repliesEl) repliesEl.textContent = turns?.replies != null ? String(turns.replies) : "0";
  if (runIdEl) runIdEl.textContent = run?.short_id || run?.id || "–";
  if (failureEl) failureEl.textContent = failureSummary || "–";

  // Diff stats (lines changed)
  const diffStatsEl = document.getElementById("lines-changed");
  if (diffStatsEl) {
    const diffStats = turns?.diff_stats;
    if (diffStats && (diffStats.insertions > 0 || diffStats.deletions > 0)) {
      const ins = diffStats.insertions || 0;
      const del = diffStats.deletions || 0;
      diffStatsEl.innerHTML = `<span class="diff-add">+${formatTokensCompact(ins)}</span> <span class="diff-del">-${formatTokensCompact(del)}</span>`;
      diffStatsEl.title = `${ins} insertions, ${del} deletions, ${diffStats.files_changed || 0} files changed`;
    } else {
      diffStatsEl.textContent = "–";
      diffStatsEl.title = "";
    }
  }

  // Agent chip (optional future use)
  const agentEl = document.getElementById("ticket-agent");
  if (agentEl) {
    agentEl.textContent = agent?.id || "–";
  }
}

async function fetchRunHistory(): Promise<FlowRun[]> {
  const runs = (await api("/api/flows/runs?flow_type=ticket_flow")) as FlowRun[];
  return Array.isArray(runs) ? runs.slice(0, 10) : [];
}

async function loadRunHistory(reason: SmartRefreshReason = "manual"): Promise<void> {
  const resolvedReason = normalizeRefreshReason(reason);
  try {
    await runHistoryRefresh.refresh(fetchRunHistory, { reason: resolvedReason });
  } catch (err) {
    flash((err as Error).message || "Failed to load run history", "error");
  }
}

function formatIso(iso: string | null): string {
  if (!iso) return "–";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleString();
}

function calcDurationFromRun(run: FlowRun): string {
  const started = run.started_at;
  const finished = run.finished_at;
  if (!started) return "–";
  const start = new Date(started).getTime();
  const end =
    finished && !Number.isNaN(new Date(finished).getTime())
      ? new Date(finished).getTime()
      : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return "–";
  return formatDuration((end - start) / 1000);
}

function renderRunHistory(runs: FlowRun[]): void {
  const container = document.getElementById("run-history-list");
  if (!container) return;
  preserveScroll(container, () => {
    if (!runs || !runs.length) {
      container.innerHTML = '<div class="muted">No runs yet.</div>';
      return;
    }
    const items = runs.map((run) => {
      const shortId = run.id ? run.id.split("-")[0] : "–";
      const status = run.status || "unknown";
      const duration = calcDurationFromRun(run);
      const started = formatIso(run.started_at);
      const current = run.current_step || "–";
      return `
        <div class="run-history-row">
          <div class="run-history-id">${shortId}</div>
          <div class="run-history-status">${status}</div>
          <div class="run-history-duration">${duration}</div>
          <div class="run-history-start">${started}</div>
          <div class="run-history-step">${current}</div>
        </div>
      `;
    });
    container.innerHTML = `
      <div class="run-history-head">
        <div>ID</div><div>Status</div><div>Duration</div><div>Started</div><div>Step</div>
      </div>
      ${items.join("")}
    `;
  }, { restoreOnNextFrame: true });
}

function buildUsageSeriesQuery(): string {
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



const usageSeriesRefresh = createSmartRefresh<UsageChartData | null>({
  getSignature: (payload) => usageSeriesSignature(payload),
  render: (payload) => {
    renderUsageChart(payload, usageChartState.segment);
  },
});

async function loadUsageSeries(reason: SmartRefreshReason = "manual"): Promise<void> {
  const resolvedReason = normalizeRefreshReason(reason);
  const container = document.getElementById("usage-chart-canvas") as HTMLElement | null;
  try {
    const data = await api(`/api/usage/series?${buildUsageSeriesQuery()}`) as UsageChartData;
    setChartLoading(container, data?.status === "loading");
    await usageSeriesRefresh.refresh(async () => data, { reason: resolvedReason });
    if (data?.status === "loading") {
      scheduleUsageSeriesRetry();
    } else {
      clearUsageSeriesRetry();
    }
  } catch (err) {
    setChartLoading(container, false);
    await usageSeriesRefresh.refresh(async () => null, { reason: resolvedReason });
    clearUsageSeriesRetry();
  }
}

function scheduleUsageSeriesRetry(): void {
  clearUsageSeriesRetry();
  usageSeriesRetryTimer = setTimeout(() => {
    loadUsageSeries();
  }, 1500);
}

function clearUsageSeriesRetry(): void {
  if (usageSeriesRetryTimer) {
    clearTimeout(usageSeriesRetryTimer);
    usageSeriesRetryTimer = null;
  }
}

function scheduleUsageSummaryRetry(): void {
  clearUsageSummaryRetry();
  usageSummaryRetryTimer = setTimeout(() => {
    loadUsage();
  }, 1500);
}

function clearUsageSummaryRetry(): void {
  if (usageSummaryRetryTimer) {
    clearTimeout(usageSummaryRetryTimer);
    usageSummaryRetryTimer = null;
  }
}

async function fetchUsageSummary(): Promise<UsageData | null> {
  try {
    const data = await api("/api/usage") as UsageData;
    const cachedUsage = loadFromCache("usage");
    const hasSummary = data && data.totals && typeof data.events === "number";
    if (data?.status === "loading") {
      if (hasSummary) {
        scheduleUsageSummaryRetry();
        return data;
      }
      if (cachedUsage) {
        scheduleUsageSummaryRetry();
        return cachedUsage as UsageData;
      }
      scheduleUsageSummaryRetry();
      return data;
    }
    clearUsageSummaryRetry();
    return data;
  } catch (err) {
    const cachedUsage = loadFromCache("usage");
    flash((err as Error).message || "Failed to load usage", "error");
    clearUsageSummaryRetry();
    if (cachedUsage) {
      return cachedUsage as UsageData;
    }
    return null;
  }
}

async function loadUsage(reason: SmartRefreshReason = "manual"): Promise<void> {
  const resolvedReason = normalizeRefreshReason(reason);
  const showLoading = resolvedReason !== "background";
  if (showLoading) setUsageLoading(true);
  try {
    await usageSummaryRefresh.refresh(fetchUsageSummary, { reason: resolvedReason });
  } catch (err) {
    flash((err as Error).message || "Failed to load usage", "error");
  } finally {
    if (showLoading) setUsageLoading(false);
  }
  await loadUsageSeries(resolvedReason);
}

function initUsageChartControls(): void {
  const segmentSelect = document.getElementById("usage-chart-segment") as HTMLSelectElement | null;
  const rangeSelect = document.getElementById("usage-chart-range") as HTMLSelectElement | null;
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

function bindAction(buttonId: string, action: () => Promise<void>): void {
  const btn = document.getElementById(buttonId) as HTMLButtonElement | null;
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      await action();
    } catch (err) {
      flash((err as Error).message || "Error", "error");
    } finally {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  });
}

export function initDashboard(): void {
  initUsageChartControls();
  // initReview(); // Removed - review.ts was deleted
  bindAction("usage-refresh", () => loadUsage("manual"));
  bindAction("analytics-refresh", async () => {
    await loadTicketAnalytics("manual");
    await loadRunHistory("manual");
  });

  const cachedUsage = loadFromCache("usage");
  if (cachedUsage) renderUsage(cachedUsage as UsageData);
  const cachedAnalytics = loadFromCache(ANALYTICS_SUMMARY_CACHE_KEY);
  if (cachedAnalytics) renderTicketAnalytics(cachedAnalytics as AnalyticsSummary);

  loadUsage("initial");
  loadTicketAnalytics("initial");
  loadRunHistory("initial");
  loadVersion();
  checkUpdateStatus();

  registerAutoRefresh("dashboard-usage", {
    callback: async (ctx) => {
      await loadUsage(mapAutoRefreshReason(ctx));
    },
    tabId: "analytics",
    interval: CONSTANTS.UI.AUTO_REFRESH_USAGE_INTERVAL,
    refreshOnActivation: true,
    immediate: false,
  });
  registerAutoRefresh("dashboard-analytics", {
    callback: async (ctx) => {
      const reason = mapAutoRefreshReason(ctx);
      await loadTicketAnalytics(reason);
      await loadRunHistory(reason);
    },
    tabId: "analytics",
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: true,
    });
}

async function loadVersion(): Promise<void> {
  const versionEl = document.getElementById("repo-version");
  if (!versionEl) return;
  try {
    const data = await api("/api/version", { method: "GET" });
    const version = (data as { asset_version?: string })?.asset_version || "";
    versionEl.textContent = version ? `v${version}` : "v–";
  } catch (_err) {
    versionEl.textContent = "v–";
  }
}

interface UpdateStatusResponse {
  status?: string;
  message?: string;
  at?: string | number;
}

async function checkUpdateStatus(): Promise<void> {
  try {
    const data = await api("/system/update/status", { method: "GET" }) as UpdateStatusResponse;
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
