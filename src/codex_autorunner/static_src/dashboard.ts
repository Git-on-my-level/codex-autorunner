import { api, flash, statusPill } from "./utils.js";
import { saveToCache, loadFromCache } from "./cache.js";
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { preserveScroll } from "./preserve.js";
import { createSmartRefresh, type SmartRefreshReason } from "./smartRefresh.js";

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

function formatTokensCompact(val: number | string | null | undefined): string {
  if (val === null || val === undefined) return "–";
  const num = Number(val);
  if (Number.isNaN(num)) return String(val);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(0)}k`;
  return num.toLocaleString();
}

function formatTokensAxis(val: number | string | null | undefined): string {
  if (val === null || val === undefined) return "0";
  const num = Number(val);
  if (Number.isNaN(num)) return "0";
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return Math.round(num).toString();
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

interface SeriesEntry {
  key?: string;
  model?: string | null;
  token_type?: string | null;
  total?: number;
  values?: number[];
}

interface UsageChartData {
  buckets?: string[];
  series?: SeriesEntry[];
  status?: string;
}

function usageSeriesSignature(data: UsageChartData | null): string {
  if (!data) return "none";
  const buckets = data.buckets || [];
  const series = data.series || [];
  const seriesSig = series
    .map((entry) => {
      const values = entry.values || [];
      return [
        entry.key ?? "",
        entry.model ?? "",
        entry.token_type ?? "",
        entry.total ?? "",
        values.join(","),
      ].join(":");
    })
    .join("|");
  return `${data.status || ""}::${buckets.join(",")}::${seriesSig}`;
}

const usageSeriesRefresh = createSmartRefresh<UsageChartData | null>({
  getSignature: (payload) => usageSeriesSignature(payload),
  render: (payload) => {
    renderUsageChart(payload);
  },
});

function renderUsageChart(data: UsageChartData | null): void {
  const container = document.getElementById("usage-chart-canvas") as HTMLElement | null;
  if (!container) return;
  const buckets = data?.buckets || [];
  const series = data?.series || [];
  const isLoading = data?.status === "loading";
  if (!buckets.length || !series.length) {
    (container as UsageChartElement).__usageChartBound = false;
    container.innerHTML = isLoading
      ? '<div class="usage-chart-empty">Loading…</div>'
      : '<div class="usage-chart-empty">No data</div>';
    return;
  }

  const { width, height } = getChartSize(container, 320, 88);
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
    "#373",
  ];

  const { series: displaySeries } = normalizeSeries(
    limitSeries(series, 4, "rest").series,
    buckets.length
  );

  let scaleMax = 1;
  const totals = new Array(buckets.length).fill(0);
  displaySeries.forEach((entry) => {
    (entry.values || []).forEach((value, i) => {
      totals[i] += value;
    });
  });
  scaleMax = Math.max(...totals, 1);

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMinYMin meet" role="img" aria-label="Token usage trend">`;
  svg += `
    <defs></defs>
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

  const count = buckets.length;
  const barWidth = count ? chartWidth / count : chartWidth;
  const gap = Math.max(1, Math.round(barWidth * 0.2));
  const usableWidth = Math.max(1, barWidth - gap);
  if (usageChartState.segment === "none") {
    const values = displaySeries[0]?.values || [];
    values.forEach((value, i) => {
      const x = padding + i * barWidth + gap / 2;
      const h = (value / scaleMax) * chartHeight;
      const y = padding + chartHeight - h;
      svg += `<rect x="${x}" y="${y}" width="${usableWidth}" height="${h}" fill="#6cf5d8" opacity="0.75" rx="2" />`;
    });
  } else {
    const accum = new Array(count).fill(0);
    displaySeries.forEach((entry, idx) => {
      const color = colors[idx % colors.length];
      const values = entry.values || [];
      values.forEach((value, i) => {
        if (!value) return;
        const base = accum[i];
        accum[i] += value;
        const h = (value / scaleMax) * chartHeight;
        const y = padding + chartHeight - (base / scaleMax) * chartHeight - h;
        const x = padding + i * barWidth + gap / 2;
        svg += `<rect x="${x}" y="${y}" width="${usableWidth}" height="${h}" fill="${color}" opacity="0.55" rx="2" />`;
      });
    });
  }

  svg += "</svg>";
  (container as UsageChartElement).__usageChartBound = false;
  container.innerHTML = svg;
  attachUsageChartInteraction(container, {
    buckets,
    series: displaySeries,
    segment: usageChartState.segment,
    scaleMax,
    width,
    height,
    padding,
    chartWidth,
    chartHeight,
  });
}

function getChartSize(container: HTMLElement, fallbackWidth: number, fallbackHeight: number): { width: number; height: number } {
  const rect = container.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width || fallbackWidth));
  const height = Math.max(1, Math.round(rect.height || fallbackHeight));
  return { width, height };
}

function limitSeries(series: SeriesEntry[], maxSeries: number, restKey: string): { series: SeriesEntry[] } {
  if (series.length <= maxSeries) return { series };
  const sorted = [...series].sort((a, b) => (b.total || 0) - (a.total || 0));
  const top = sorted.slice(0, maxSeries).filter((entry) => (entry.total || 0) > 0);
  const rest = sorted.slice(maxSeries);
  if (!rest.length) return { series: top };
  const values = new Array((top[0]?.values || []).length).fill(0);
  rest.forEach((entry) => {
    (entry.values || []).forEach((value, i) => {
      values[i] += value;
    });
  });
  const total = values.reduce((sum, value) => sum + value, 0);
  if (total > 0) {
    top.push({ key: restKey, model: null, token_type: null, total, values });
  }
  return { series: top.length ? top : series };
}

function normalizeSeries(series: SeriesEntry[], length: number): { series: SeriesEntry[] } {
  const normalized = series.map((entry) => {
    const values = (entry.values || []).slice(0, length);
    while (values.length < length) values.push(0);
    return { ...entry, values, total: values.reduce((sum, v) => sum + v, 0) };
  });
  return { series: normalized };
}

function setChartLoading(container: HTMLElement | null, loading: boolean): void {
  if (!container) return;
  container.classList.toggle("loading", loading);
}

interface UsageChartElement extends HTMLElement {
  __usageChartBound?: boolean;
  __usageChartState?: ChartInteractionState;
}

interface ChartInteractionState {
  buckets: string[];
  series: SeriesEntry[];
  segment: string;
  scaleMax: number;
  width: number;
  height: number;
  padding: number;
  chartWidth: number;
  chartHeight: number;
}

function attachUsageChartInteraction(container: HTMLElement, state: ChartInteractionState): void {
  (container as UsageChartElement).__usageChartState = state;
  if ((container as UsageChartElement).__usageChartBound) return;
  (container as UsageChartElement).__usageChartBound = true;

  const focus = document.createElement("div");
  focus.className = "usage-chart-focus";
  const dot = document.createElement("div");
  dot.className = "usage-chart-dot";
  const tooltip = document.createElement("div");
  tooltip.className = "usage-chart-tooltip";
  container.appendChild(focus);
  container.appendChild(dot);
  container.appendChild(tooltip);

  const updateTooltip = (event: PointerEvent) => {
    const chartState = (container as UsageChartElement).__usageChartState;
    if (!chartState) return;
    const rect = container.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const normalizedX = (x / rect.width) * chartState.width;
    const count = chartState.buckets.length;
    const usableWidth = chartState.chartWidth;
    const localX = Math.min(
      Math.max(normalizedX - chartState.padding, 0),
      usableWidth
    );
    const barWidth = count ? usableWidth / count : usableWidth;
    const index = Math.floor(localX / barWidth);
    const clampedIndex = Math.max(
      0,
      Math.min(chartState.buckets.length -1, index)
    );
    const xPos =
      chartState.padding + clampedIndex * barWidth + barWidth / 2;

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
    const rows: string[] = [];
    rows.push(
      `<div class="usage-chart-tooltip-row"><span>Total</span><span>${formatTokensCompact(
        totals
      )}</span></div>`
    );

    if (chartState.segment !== "none") {
      const ranked = chartState.series
        .map((entry) => ({
          key: entry.key || "unknown",
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
    const tooltipTop = 6;
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
