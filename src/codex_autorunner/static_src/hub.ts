import {
  api,
  flash,
  statusPill,
  resolvePath,
  escapeHtml,
  confirmModal,
  inputModal,
  openModal,
} from "./utils.js";
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";
import { HUB_BASE } from "./env.js";
import { preserveScroll } from "./preserve.js";

interface HubRepo {
  id: string;
  path: string;
  display_name: string;
  enabled: boolean;
  auto_run: boolean;
  kind: "base" | "worktree";
  worktree_of: string | null;
  branch: string | null;
  exists_on_disk: boolean;
  is_clean: boolean | null;
  initialized: boolean;
  init_error: string | null;
  status: string;
  lock_status: string;
  last_run_id: number | null;
  last_exit_code: number | null;
  last_run_started_at: string | null;
  last_run_finished_at: string | null;
  runner_pid: number | null;
  mounted: boolean;
  mount_error?: string | null;
}

interface HubData {
  repos: HubRepo[];
  last_scan_at: string | null;
}

interface HubUsageRepo {
  id: string;
  totals?: {
    total_tokens?: number;
    input_tokens?: number;
    cached_input_tokens?: number;
  };
  events?: number;
}

interface HubUsageData {
  repos?: HubUsageRepo[];
  unmatched?: {
    events?: number;
    totals?: {
      total_tokens?: number;
    };
  };
  codex_home?: string;
  status?: string;
}

interface SessionCachePayload<T> {
  at: number;
  value: T;
}

interface HubJob {
  job_id: string;
  status?: string;
  error?: string;
  result?: {
    mounted?: boolean;
    id?: string;
  };
}

interface SeriesEntry {
  key?: string;
  repo?: string | null;
  token_type?: string | null;
  total?: number;
  values?: number[];
}

interface HubChartData {
  buckets?: string[];
  series?: SeriesEntry[];
  status?: string;
}

interface UpdateCheckResponse {
  update_available?: boolean;
  message?: string;
}

interface UpdateResponse {
  message?: string;
}

let hubData: HubData = { repos: [], last_scan_at: null };
const prefetchedUrls = new Set<string>();
let hubInboxHydrated = false;

const HUB_CACHE_TTL_MS = 30000;
const HUB_CACHE_KEY = `car:hub:${HUB_BASE || "/"}`;
const HUB_USAGE_CACHE_KEY = `car:hub-usage:${HUB_BASE || "/"}`;
const HUB_REFRESH_ACTIVE_MS = 5000;
const HUB_REFRESH_IDLE_MS = 30000;

let lastHubAutoRefreshAt = 0;

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
const hubInboxList = document.getElementById("hub-inbox-list");
const hubInboxRefresh = document.getElementById("hub-inbox-refresh") as HTMLButtonElement | null;
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
const HUB_JOB_POLL_INTERVAL_MS = 1200;
const HUB_JOB_TIMEOUT_MS = 180000;

interface HubUsageChartState {
  segment: string;
  bucket: string;
  windowDays: number;
}

const hubUsageChartState: HubUsageChartState = {
  segment: "none",
  bucket: "day",
  windowDays: 30,
};

let hubUsageSeriesRetryTimer: ReturnType<typeof setTimeout> | null = null;
let hubUsageSummaryRetryTimer: ReturnType<typeof setTimeout> | null = null;

function saveSessionCache<T>(key: string, value: T): void {
  try {
    const payload: SessionCachePayload<T> = { at: Date.now(), value };
    sessionStorage.setItem(key, JSON.stringify(payload));
  } catch (_err) {
    // Ignore storage errors; cache is best-effort.
  }
}

function loadSessionCache<T>(key: string, maxAgeMs: number): T | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const payload = JSON.parse(raw) as SessionCachePayload<T>;
    if (!payload || typeof payload.at !== "number") return null;
    if (maxAgeMs && Date.now() - payload.at > maxAgeMs) return null;
    return payload.value;
  } catch (_err) {
    return null;
  }
}

function formatRunSummary(repo: HubRepo): string {
  if (!repo.initialized) return "Not initialized";
  if (!repo.exists_on_disk) return "Missing on disk";
  if (!repo.last_run_id) return "No runs yet";
  const exit =
    repo.last_exit_code === null || repo.last_exit_code === undefined
      ? ""
      : ` exit:${repo.last_exit_code}`;
  return `#${repo.last_run_id}${exit}`;
}

function formatLastActivity(repo: HubRepo): string {
  if (!repo.initialized) return "";
  const time = repo.last_run_finished_at || repo.last_run_started_at;
  if (!time) return "";
  return formatTimeCompact(time);
}

function setButtonLoading(scanning: boolean): void {
  const buttons = [
    document.getElementById("hub-scan"),
    document.getElementById("hub-quick-scan"),
    document.getElementById("hub-refresh"),
  ] as (HTMLButtonElement | null)[];
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface PollHubJobOptions {
  timeoutMs?: number;
}

async function pollHubJob(jobId: string, { timeoutMs = HUB_JOB_TIMEOUT_MS }: PollHubJobOptions = {}): Promise<HubJob> {
  const start = Date.now();
  for (;;) {
    const job = await api(`/hub/jobs/${jobId}`, { method: "GET" }) as HubJob;
    if (job.status === "succeeded") return job;
    if (job.status === "failed") {
      const err = job.error || "Hub job failed";
      throw new Error(err);
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error("Hub job timed out");
    }
    await sleep(HUB_JOB_POLL_INTERVAL_MS);
  }
}

interface StartHubJobOptions {
  body?: unknown;
  startedMessage?: string;
}

async function startHubJob(path: string, { body, startedMessage }: StartHubJobOptions = {}): Promise<HubJob> {
  const job = await api(path, { method: "POST", body }) as { job_id: string };
  if (startedMessage) {
    flash(startedMessage);
  }
  return pollHubJob(job.job_id);
}

function formatTimeCompact(isoString: string | null): string {
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

function renderSummary(repos: HubRepo[]): void {
  const running = repos.filter((r) => r.status === "running").length;
  const missing = repos.filter((r) => !r.exists_on_disk).length;
  if (totalEl) totalEl.textContent = repos.length.toString();
  if (runningEl) runningEl.textContent = running.toString();
  if (missingEl) missingEl.textContent = missing.toString();
  if (lastScanEl) {
    lastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
  }
}

function formatTokensCompact(val: number | string | null | undefined): string {
  if (val === null || val === undefined) return "0";
  const num = Number(val);
  if (Number.isNaN(num)) return String(val);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(0)}k`;
  return num.toLocaleString();
}

function formatTokensAxis(val: number | string): string {
  const num = Number(val);
  if (Number.isNaN(num)) return "0";
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return Math.round(num).toString();
}

function renderHubUsage(data: HubUsageData | null): void {
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
      <span class="hub-usage-chip-name">${escapeHtml(repo.id)}</span>
      <span class="hub-usage-chip-total">${escapeHtml(
        formatTokensCompact(totals.total_tokens)
      )}</span>
      <span class="hub-usage-chip-meta">${escapeHtml(
        `${repo.events ?? 0}ev · ${cachePercent}%↻`
      )}</span>
    `;
    hubUsageList.appendChild(div);
  });
  if (data.unmatched && data.unmatched.events) {
    const div = document.createElement("div");
    div.className = "hub-usage-chip hub-usage-chip-unmatched";
    const totals = data.unmatched.totals || {};
    div.innerHTML = `
      <span class="hub-usage-chip-name">other</span>
      <span class="hub-usage-chip-total">${escapeHtml(
        formatTokensCompact(totals.total_tokens)
      )}</span>
      <span class="hub-usage-chip-meta">${escapeHtml(
        `${data.unmatched.events}ev`
      )}</span>
    `;
    hubUsageList.appendChild(div);
  }
}

function scheduleHubUsageSummaryRetry(): void {
  clearHubUsageSummaryRetry();
  hubUsageSummaryRetryTimer = setTimeout(() => {
    loadHubUsage();
  }, 1500);
}

function clearHubUsageSummaryRetry(): void {
  if (hubUsageSummaryRetryTimer) {
    clearTimeout(hubUsageSummaryRetryTimer);
    hubUsageSummaryRetryTimer = null;
  }
}

interface HandleHubUsagePayloadOptions {
  cachedUsage?: HubUsageData | null;
  allowRetry?: boolean;
}

function handleHubUsagePayload(data: HubUsageData | null, { cachedUsage, allowRetry }: HandleHubUsagePayloadOptions): boolean {
  const hasSummary = data && Array.isArray(data.repos);
  if (data?.status === "loading") {
    if (hasSummary) {
      renderHubUsage(data);
    } else if (cachedUsage) {
      renderHubUsage(cachedUsage);
    } else {
      renderHubUsage(data);
    }
    if (allowRetry) scheduleHubUsageSummaryRetry();
    return hasSummary;
  }
  if (hasSummary) {
    renderHubUsage(data);
    clearHubUsageSummaryRetry();
    return true;
  }
  if (cachedUsage) {
    renderHubUsage(cachedUsage);
  } else {
    renderHubUsage(null);
  }
  return false;
}

interface LoadHubUsageOptions {
  silent?: boolean;
  allowRetry?: boolean;
}

async function loadHubUsage({ silent = false, allowRetry = true }: LoadHubUsageOptions = {}): Promise<void> {
  if (!silent && hubUsageRefresh) (hubUsageRefresh as HTMLButtonElement).disabled = true;
  try {
    const data = await api("/hub/usage") as HubUsageData;
    const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    const shouldCache = handleHubUsagePayload(data, {
      cachedUsage,
      allowRetry,
    });
    loadHubUsageSeries();
    if (shouldCache) {
      saveSessionCache(HUB_USAGE_CACHE_KEY, data);
    }
  } catch (err) {
    const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (cachedUsage) {
      renderHubUsage(cachedUsage);
    } else {
      renderHubUsage(null);
    }
    if (!silent) {
      flash((err as Error).message || "Failed to load usage", "error");
    }
    clearHubUsageSummaryRetry();
  } finally {
    if (!silent && hubUsageRefresh) (hubUsageRefresh as HTMLButtonElement).disabled = false;
  }
}

function buildHubUsageSeriesQuery(): string {
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

function renderHubUsageChart(data: HubChartData | null): void {
  if (!hubUsageChartCanvas) return;
  const buckets = data?.buckets || [];
  const series = data?.series || [];
  const isLoading = data?.status === "loading";
  if (!buckets.length || !series.length) {
    (hubUsageChartCanvas as unknown as { __usageChartBound: boolean }).__usageChartBound = false;
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

  const { series: displaySeries } = normalizeSeries(
    limitSeries(series, 6, "rest").series,
    buckets.length
  );

  const totals = new Array(buckets.length).fill(0);
  displaySeries.forEach((entry) => {
    (entry.values || []).forEach((value, i) => {
      totals[i] += value;
    });
  });
  const scaleMax = Math.max(...totals, 1);

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMinYMin meet" role="img" aria-label="Hub usage trend">`;
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
  svg += `<text x="${padding}" y="${padding + 12}" fill="rgba(203, 213, 225, 0.7)" font-size="9">${maxLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight / 2 + 4
  }" fill="rgba(203, 213, 225, 0.6)" font-size="9">${midLabel}</text>`;
  svg += `<text x="${padding}" y="${
    padding + chartHeight + 2
  }" fill="rgba(203, 213, 225, 0.5)" font-size="9">0</text>`;

  const count = buckets.length;
  const barWidth = count ? chartWidth / count : chartWidth;
  const gap = Math.max(1, Math.round(barWidth * 0.2));
  const usableWidth = Math.max(1, barWidth - gap);
  if (hubUsageChartState.segment === "none") {
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
  (hubUsageChartCanvas as unknown as { __usageChartBound: boolean }).__usageChartBound = false;
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
    top.push({ key: restKey, repo: null, token_type: null, total, values });
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

function attachHubUsageChartInteraction(container: HTMLElement, state: ChartInteractionState): void {
  (container as unknown as { __usageChartState: ChartInteractionState }).__usageChartState = state;
  if ((container as unknown as { __usageChartBound: boolean }).__usageChartBound) return;
  (container as unknown as { __usageChartBound: boolean }).__usageChartBound = true;

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
    const chartState = (container as unknown as { __usageChartState: ChartInteractionState }).__usageChartState;
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
      Math.min(chartState.buckets.length - 1, index)
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
      `<div class="usage-chart-tooltip-row"><span>Total</span><span>${escapeHtml(
        formatTokensCompact(totals)
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
        .slice(0, 6);
      ranked.forEach((entry) => {
        rows.push(
          `<div class="usage-chart-tooltip-row"><span>${escapeHtml(
            entry.key
          )}</span><span>${escapeHtml(
            formatTokensCompact(entry.value)
          )}</span></div>`
        );
      });
    }

    tooltip.innerHTML = `<div class="usage-chart-tooltip-title">${escapeHtml(
      bucketLabel
    )}</div>${rows.join("")}`;

    const tooltipRect = tooltip.getBoundingClientRect();
    let tooltipLeft = x + 12;
    if (tooltipLeft + tooltipRect.width > rect.width) {
      tooltipLeft = x - tooltipRect.width - 12;
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

async function loadHubUsageSeries(): Promise<void> {
  if (!hubUsageChartCanvas) return;
  try {
    const data = await api(`/hub/usage/series?${buildHubUsageSeriesQuery()}`) as HubChartData;
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

function scheduleHubUsageSeriesRetry(): void {
  clearHubUsageSeriesRetry();
  hubUsageSeriesRetryTimer = setTimeout(() => {
    loadHubUsageSeries();
  }, 1500);
}

function clearHubUsageSeriesRetry(): void {
  if (hubUsageSeriesRetryTimer) {
    clearTimeout(hubUsageSeriesRetryTimer);
    hubUsageSeriesRetryTimer = null;
  }
}

function initHubUsageChartControls(): void {
  if (hubUsageChartRange) {
    (hubUsageChartRange as HTMLSelectElement).value = String(hubUsageChartState.windowDays);
    hubUsageChartRange.addEventListener("change", () => {
      const value = Number((hubUsageChartRange as HTMLSelectElement).value);
      hubUsageChartState.windowDays = Number.isNaN(value)
        ? hubUsageChartState.windowDays
        : value;
      loadHubUsageSeries();
    });
  }
  if (hubUsageChartSegment) {
    (hubUsageChartSegment as HTMLSelectElement).value = hubUsageChartState.segment;
    hubUsageChartSegment.addEventListener("change", () => {
      hubUsageChartState.segment = (hubUsageChartSegment as HTMLSelectElement).value;
      loadHubUsageSeries();
    });
  }
}

const UPDATE_TARGET_LABELS: Record<string, string> = {
  both: "web + Telegram",
  web: "web only",
  telegram: "Telegram only",
};

type UpdateTarget = "both" | "web" | "telegram";

function normalizeUpdateTarget(value: unknown): UpdateTarget {
  if (!value) return "both";
  if (value === "both" || value === "web" || value === "telegram") return value as UpdateTarget;
  return "both";
}

function getUpdateTarget(selectId: string | null): UpdateTarget {
  const select = selectId ? (document.getElementById(selectId) as HTMLSelectElement | null) : null;
  return normalizeUpdateTarget(select ? select.value : "both");
}

function describeUpdateTarget(target: UpdateTarget): string {
  return UPDATE_TARGET_LABELS[target] || UPDATE_TARGET_LABELS.both;
}

async function handleSystemUpdate(btnId: string, targetSelectId: string | null): Promise<void> {
  const btn = document.getElementById(btnId) as HTMLButtonElement | null;
  if (!btn) return;

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Checking...";
  const updateTarget = getUpdateTarget(targetSelectId);
  const targetLabel = describeUpdateTarget(updateTarget);

  let check: UpdateCheckResponse | undefined;
  try {
    check = await api("/system/update/check") as UpdateCheckResponse;
  } catch (err) {
    check = { update_available: true, message: (err as Error).message || "Unable to check for updates." };
  }

  if (!check?.update_available) {
    flash(check?.message || "No update available.", "info");
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  const restartNotice =
    updateTarget === "telegram"
      ? "The Telegram bot will restart."
      : "The service will restart.";
  const confirmed = await confirmModal(
    `${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`
  );
  if (!confirmed) {
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  btn.textContent = "Updating...";

  try {
    const res = await api("/system/update", {
      method: "POST",
      body: { target: updateTarget },
    }) as UpdateResponse;
    flash(res.message || `Update started (${targetLabel}).`, "success");
    if (updateTarget === "telegram") {
      btn.disabled = false;
      btn.textContent = originalText;
      return;
    }
    document.body.style.pointerEvents = "none";
    setTimeout(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("v", String(Date.now()));
      window.location.replace(url.toString());
    }, 8000);
  } catch (err) {
    flash((err as Error).message || "Update failed", "error");
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function initHubSettings(): void {
  const settingsBtn = document.getElementById("hub-settings") as HTMLButtonElement | null;
  const modal = document.getElementById("hub-settings-modal");
  const closeBtn = document.getElementById("hub-settings-close");
  const updateBtn = document.getElementById("hub-update-btn") as HTMLButtonElement | null;
  const updateTarget = document.getElementById("hub-update-target") as HTMLSelectElement | null;
  let closeModal: (() => void) | null = null;

  const hideModal = () => {
    if (closeModal) {
      const close = closeModal;
      closeModal = null;
      close();
    }
  };

  if (settingsBtn && modal) {
    settingsBtn.addEventListener("click", () => {
      const triggerEl = document.activeElement;
      hideModal();
      closeModal = openModal(modal, {
        initialFocus: closeBtn || updateBtn || modal,
        returnFocusTo: triggerEl as HTMLElement | null,
        onRequestClose: hideModal,
      });
    });
  }

  if (closeBtn && modal) {
    closeBtn.addEventListener("click", () => {
      hideModal();
    });
  }

  if (updateBtn) {
    updateBtn.addEventListener("click", () =>
      handleSystemUpdate("hub-update-btn", updateTarget ? updateTarget.id : null)
    );
  }
}

interface RepoAction {
  key: string;
  label: string;
  kind: string;
  title?: string;
  disabled?: boolean;
}

function buildActions(repo: HubRepo): RepoAction[] {
  const actions: RepoAction[] = [];
  const missing = !repo.exists_on_disk;
  const kind = repo.kind || "base";
  if (!missing && repo.mount_error) {
    actions.push({ key: "init", label: "Retry mount", kind: "primary" });
  } else if (!missing && repo.init_error) {
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
    const clean = repo.is_clean;
    const syncDisabled = clean !== true;
    const syncTitle = syncDisabled
      ? "Working tree must be clean to sync main"
      : "Switch to main and pull latest";
    actions.push({
      key: "sync_main",
      label: "Sync main",
      kind: "ghost",
      title: syncTitle,
      disabled: syncDisabled,
    });
  }
  if (!missing && kind === "worktree") {
    actions.push({
      key: "cleanup_worktree",
      label: "Cleanup",
      kind: "ghost",
      title: "Remove worktree and delete branch",
    });
  }
  if (kind === "base") {
    actions.push({ key: "remove_repo", label: "Remove", kind: "danger" });
  }
  return actions;
}

function buildMountBadge(repo: HubRepo): string {
  if (!repo) return "";
  const missing = !repo.exists_on_disk;
  let label = "";
  let className = "pill pill-small";
  let title = "";
  if (missing) {
    label = "missing";
    className += " pill-error";
    title = "Repo path not found on disk";
  } else if (repo.mount_error) {
    label = "mount error";
    className += " pill-error";
    title = repo.mount_error;
  } else if (repo.mounted === true) {
    label = "mounted";
    className += " pill-idle";
  } else {
    label = "not mounted";
    className += " pill-warn";
  }
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `<span class="${className} hub-mount-pill"${titleAttr}>${escapeHtml(
    label
  )}</span>`;
}

function inferBaseId(repo: HubRepo | null): string | null {
  if (!repo) return null;
  if (repo.worktree_of) return repo.worktree_of;
  if (typeof repo.id === "string" && repo.id.includes("--")) {
    return repo.id.split("--")[0];
  }
  return null;
}

function renderRepos(repos: HubRepo[]): void {
  if (!repoListEl) return;
  repoListEl.innerHTML = "";
  if (!repos.length) {
    repoListEl.innerHTML =
      '<div class="hub-empty muted">No repos discovered yet. Run a scan or create a new repo.</div>';
    return;
  }

  const bases = repos.filter((r) => (r.kind || "base") === "base");
  const worktrees = repos.filter((r) => (r.kind || "base") === "worktree");
  const byBase = new Map<string, { base: HubRepo; worktrees: HubRepo[] }>();
  bases.forEach((b) => byBase.set(b.id, { base: b, worktrees: [] }));
  const orphanWorktrees: HubRepo[] = [];
  worktrees.forEach((w) => {
    const baseId = inferBaseId(w);
    if (baseId && byBase.has(baseId)) {
      byBase.get(baseId)!.worktrees.push(w);
    } else {
      orphanWorktrees.push(w);
    }
  });

  const orderedGroups = [...byBase.values()].sort((a, b) =>
    String(a.base?.id || "").localeCompare(String(b.base?.id || ""))
  );

  const renderRepoCard = (repo: HubRepo, { isWorktreeRow = false } = {}): void => {
    const card = document.createElement("div");
    card.className = isWorktreeRow
      ? "hub-repo-card hub-worktree-card"
      : "hub-repo-card";
    card.dataset.repoId = repo.id;

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
            escapeHtml(action.key)
          }" data-repo="${escapeHtml(repo.id)}"${
            action.title ? ` title="${escapeHtml(action.title)}"` : ""
          }${action.disabled ? " disabled" : ""}>${escapeHtml(
            action.label
          )}</button>`
      )
      .join("");

    const mountBadge = buildMountBadge(repo);
    const lockBadge =
      repo.lock_status && repo.lock_status !== "unlocked"
        ? `<span class="pill pill-small pill-warn">${escapeHtml(
            repo.lock_status.replace("_", " ")
          )}</span>`
        : "";
    const initBadge = !repo.initialized
      ? '<span class="pill pill-small pill-warn">uninit</span>'
      : "";

    let noteText = "";
    if (!repo.exists_on_disk) {
      noteText = "Missing on disk";
    } else if (repo.init_error) {
      noteText = repo.init_error;
    } else if (repo.mount_error) {
      noteText = `Cannot open: ${repo.mount_error}`;
    }
    const note = noteText
      ? `<div class="hub-repo-note">${escapeHtml(noteText)}</div>`
      : "";

    const openIndicator = canNavigate
      ? '<span class="hub-repo-open-indicator">→</span>'
      : "";

    const runSummary = formatRunSummary(repo);
    const lastActivity = formatLastActivity(repo);
    const infoItems: string[] = [];
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
        ? `<span class="hub-repo-info-line">${escapeHtml(
            infoItems.join(" · ")
           )}</span>`
        : "";

    card.innerHTML = `
      <div class="hub-repo-row">
        <div class="hub-repo-left">
            <span class="pill pill-small hub-status-pill">${escapeHtml(
              repo.status
            )}</span>
            ${mountBadge}
            ${lockBadge}
            ${initBadge}
          </div>
        <div class="hub-repo-center">
          <span class="hub-repo-title">${escapeHtml(
            repo.display_name
          )}</span>
          <div class="hub-repo-subline">
            ${infoLine}
          </div>
        </div>
        <div class="hub-repo-right">
          ${actions || ""}
          ${openIndicator}
        </div>
      </div>
      ${note}
    `;

    const statusEl = card.querySelector(".hub-status-pill") as HTMLElement | null;
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
          const tmp = document.createElement("div");
          tmp.className = "hub-worktree-row-inner";
          list.appendChild(tmp);
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

function renderReposWithScroll(repos: HubRepo[]): void {
  preserveScroll(repoListEl, () => {
    renderRepos(repos);
  }, { restoreOnNextFrame: true });
}

async function refreshHub(): Promise<void> {
  setButtonLoading(true);
  try {
    const data = await api("/hub/repos", { method: "GET" }) as HubData;
    hubData = data;
    markHubRefreshed();
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(data.repos || []);
    renderReposWithScroll(data.repos || []);
    await loadHubInbox().catch(() => {});
    await loadHubUsage().catch(() => {});
  } catch (err) {
    flash((err as Error).message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

interface HubInboxItem {
  repo_id: string;
  repo_display_name?: string;
  run_id: string;
  status?: string;
  message?: {
    mode?: string;
    title?: string | null;
    body?: string | null;
  };
  open_url?: string;
}

async function loadHubInbox(ctx?: RefreshContext): Promise<void> {
  if (!hubInboxList) return;
  if (!hubInboxHydrated || ctx?.reason === "manual") {
    hubInboxList.textContent = "Loading…";
  }
  try {
    const payload = (await api("/hub/messages", { method: "GET" })) as { items?: HubInboxItem[] };
    const items = payload?.items || [];
    const html = !items.length
      ? '<div class="muted">No paused runs</div>'
      : items
        .map((item) => {
          const title = item.message?.title || item.message?.mode || "Message";
          const excerpt = item.message?.body ? item.message.body.slice(0, 160) : "";
          const repoLabel = item.repo_display_name || item.repo_id;
          const href = item.open_url || `/repos/${item.repo_id}/?tab=messages&run_id=${item.run_id}`;
          return `
            <a class="hub-inbox-item" href="${escapeHtml(resolvePath(href))}">
              <div class="hub-inbox-item-header">
                <span class="hub-inbox-repo">${escapeHtml(repoLabel)}</span>
                <span class="pill pill-small pill-warn">paused</span>
              </div>
              <div class="hub-inbox-title">${escapeHtml(title)}</div>
              <div class="hub-inbox-excerpt muted small">${escapeHtml(excerpt)}</div>
            </a>
          `;
        })
        .join("");
    preserveScroll(hubInboxList, () => {
      hubInboxList.innerHTML = html;
    }, { restoreOnNextFrame: true });
    hubInboxHydrated = true;
  } catch (_err) {
    preserveScroll(hubInboxList, () => {
      hubInboxList.innerHTML = "";
    }, { restoreOnNextFrame: true });
  }
}

async function triggerHubScan(): Promise<void> {
  setButtonLoading(true);
  try {
    await startHubJob("/hub/jobs/scan", { startedMessage: "Hub scan queued" });
    await refreshHub();
  } catch (err) {
    flash((err as Error).message || "Hub scan failed", "error");
  } finally {
    setButtonLoading(false);
  }
}

async function createRepo(repoId: string | null, repoPath: string | null, gitInit: boolean, gitUrl: string | null): Promise<boolean> {
  try {
    const payload: Record<string, unknown> = {};
    if (repoId) payload.id = repoId;
    if (repoPath) payload.path = repoPath;
    payload.git_init = gitInit;
    if (gitUrl) payload.git_url = gitUrl;
    const job = await startHubJob("/hub/jobs/repos", {
      body: payload,
      startedMessage: "Repo creation queued",
    });
    const label = repoId || repoPath || "repo";
    flash(`Created repo: ${label}`, "success");
    await refreshHub();
    if (job?.result?.mounted && job?.result?.id) {
      window.location.href = resolvePath(`/repos/${job.result.id}/`);
    }
    return true;
  } catch (err) {
    flash((err as Error).message || "Failed to create repo", "error");
    return false;
  }
}

let closeCreateRepoModal: (() => void) | null = null;

function hideCreateRepoModal(): void {
  if (closeCreateRepoModal) {
    const close = closeCreateRepoModal;
    closeCreateRepoModal = null;
    close();
  }
}

function showCreateRepoModal(): void {
  const modal = document.getElementById("create-repo-modal");
  if (!modal) return;
  const triggerEl = document.activeElement;
  hideCreateRepoModal();
  const input = document.getElementById("create-repo-id") as HTMLInputElement | null;
  closeCreateRepoModal = openModal(modal, {
    initialFocus: input || modal,
    returnFocusTo: triggerEl as HTMLElement | null,
    onRequestClose: hideCreateRepoModal,
  });
  if (input) {
    input.value = "";
    input.focus();
  }
  const pathInput = document.getElementById("create-repo-path") as HTMLInputElement | null;
  if (pathInput) pathInput.value = "";
  const urlInput = document.getElementById("create-repo-url") as HTMLInputElement | null;
  if (urlInput) urlInput.value = "";
  const gitCheck = document.getElementById("create-repo-git") as HTMLInputElement | null;
  if (gitCheck) gitCheck.checked = true;
}

async function handleCreateRepoSubmit(): Promise<void> {
  const idInput = document.getElementById("create-repo-id") as HTMLInputElement | null;
  const pathInput = document.getElementById("create-repo-path") as HTMLInputElement | null;
  const urlInput = document.getElementById("create-repo-url") as HTMLInputElement | null;
  const gitCheck = document.getElementById("create-repo-git") as HTMLInputElement | null;

  const repoId = idInput?.value?.trim() || null;
  const repoPath = pathInput?.value?.trim() || null;
  const gitUrl = urlInput?.value?.trim() || null;
  const gitInit = gitCheck?.checked ?? true;

  if (!repoId && !gitUrl) {
    flash("Repo ID or Git URL is required", "error");
    return;
  }

  const ok = await createRepo(repoId, repoPath, gitInit, gitUrl);
  if (ok) {
    hideCreateRepoModal();
  }
}

async function handleRepoAction(repoId: string, action: string): Promise<void> {
  const buttons = repoListEl?.querySelectorAll(
    `button[data-repo="${repoId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => (btn as HTMLButtonElement).disabled = true);
  try {
    const pathMap: Record<string, string> = {
      init: `/hub/repos/${repoId}/init`,
      sync_main: `/hub/repos/${repoId}/sync-main`,
    };
    if (action === "new_worktree") {
      const branch = await inputModal("New worktree branch name:", {
        placeholder: "feature/my-branch",
        confirmText: "Create",
      });
      if (!branch) return;
      const job = await startHubJob("/hub/jobs/worktrees/create", {
        body: { base_repo_id: repoId, branch },
        startedMessage: "Worktree creation queued",
      });
      const created = job?.result;
      flash(`Created worktree: ${created?.id || branch}`, "success");
      await refreshHub();
      if (created?.mounted) {
        window.location.href = resolvePath(`/repos/${created.id}/`);
      }
      return;
    }
    if (action === "cleanup_worktree") {
      const displayName = repoId.includes("--")
        ? repoId.split("--").pop()
        : repoId;
      const ok = await confirmModal(
        `Remove worktree "${displayName}"? This will delete the worktree directory and its branch.`,
        { confirmText: "Remove", danger: true }
      );
      if (!ok) return;
      await startHubJob("/hub/jobs/worktrees/cleanup", {
        body: {
          worktree_repo_id: repoId,
          archive: true,
          force_archive: false,
          archive_note: null,
        },
        startedMessage: "Worktree cleanup queued",
      });
      flash(`Removed worktree: ${repoId}`, "success");
      await refreshHub();
      return;
    }
    if (action === "remove_repo") {
      const check = await api(`/hub/repos/${repoId}/remove-check`, {
        method: "GET",
      });
      const warnings: string[] = [];
      const dirty = (check as { is_clean?: boolean }).is_clean === false;
      if (dirty) {
        warnings.push("Working tree has uncommitted changes.");
      }
      const upstream = (check as { upstream?: { has_upstream?: boolean; ahead?: number; behind?: number } }).upstream;
      const hasUpstream = upstream?.has_upstream === false;
      if (hasUpstream) {
        warnings.push("No upstream tracking branch is configured.");
      }
      const ahead = Number(upstream?.ahead || 0);
      if (ahead > 0) {
        warnings.push(
          `Local branch is ahead of upstream by ${ahead} commit(s).`
        );
      }
      const behind = Number(upstream?.behind || 0);
      if (behind > 0) {
        warnings.push(
          `Local branch is behind upstream by ${behind} commit(s).`
        );
      }
      const worktrees = Array.isArray((check as { worktrees?: string[] }).worktrees) ? (check as { worktrees?: string[] }).worktrees : [];
      if (worktrees.length) {
        warnings.push(`This repo has ${worktrees.length} worktree(s).`);
      }

      const messageParts = [
        `Remove repo "${repoId}" and delete its local directory?`,
      ];
      if (warnings.length) {
        messageParts.push("", "Warnings:", ...warnings.map((w) => `- ${w}`));
      }
      if (worktrees.length) {
        messageParts.push(
          "",
          "Worktrees to delete:",
          ...worktrees.map((w) => `- ${w}`)
        );
      }

      const ok = await confirmModal(messageParts.join("\n"), {
        confirmText: "Remove",
        danger: true,
      });
      if (!ok) return;
      const needsForce = dirty || ahead > 0;
      if (needsForce) {
        const forceOk = await confirmModal(
          "This repo has uncommitted or unpushed changes. Remove anyway?",
          { confirmText: "Remove anyway", danger: true }
        );
        if (!forceOk) return;
      }
      await startHubJob(`/hub/jobs/repos/${repoId}/remove`, {
        body: {
          force: needsForce,
          delete_dir: true,
          delete_worktrees: worktrees.length > 0,
        },
        startedMessage: "Repo removal queued",
      });
      flash(`Removed repo: ${repoId}`, "success");
      await refreshHub();
      return;
    }

    const path = pathMap[action];
    if (!path) return;
    await api(path, { method: "POST" });
    flash(`${action} sent to ${repoId}`, "success");
    await refreshHub();
  } catch (err) {
    flash((err as Error).message || "Hub action failed", "error");
  } finally {
    buttons?.forEach((btn) => (btn as HTMLButtonElement).disabled = false);
  }
}

function attachHubHandlers(): void {
  initHubSettings();
  const scanBtn = document.getElementById("hub-scan") as HTMLButtonElement | null;
  const refreshBtn = document.getElementById("hub-refresh") as HTMLButtonElement | null;
  const quickScanBtn = document.getElementById("hub-quick-scan") as HTMLButtonElement | null;
  const newRepoBtn = document.getElementById("hub-new-repo") as HTMLButtonElement | null;
  const createCancelBtn = document.getElementById("create-repo-cancel") as HTMLButtonElement | null;
  const createSubmitBtn = document.getElementById("create-repo-submit") as HTMLButtonElement | null;
  const createRepoId = document.getElementById("create-repo-id") as HTMLInputElement | null;

  if (scanBtn) {
    scanBtn.addEventListener("click", () => triggerHubScan());
  }
  if (quickScanBtn) {
    quickScanBtn.addEventListener("click", () => triggerHubScan());
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => refreshHub());
  }
  if (hubUsageRefresh) {
    hubUsageRefresh.addEventListener("click", () => loadHubUsage());
  }

  if (newRepoBtn) {
    newRepoBtn.addEventListener("click", showCreateRepoModal);
  }
  if (createCancelBtn) {
    createCancelBtn.addEventListener("click", hideCreateRepoModal);
  }
  if (createSubmitBtn) {
    createSubmitBtn.addEventListener("click", handleCreateRepoSubmit);
  }

  if (createRepoId) {
    createRepoId.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleCreateRepoSubmit();
      }
    });
  }

  if (repoListEl) {
    repoListEl.addEventListener("click", (event) => {
        const target = event.target as HTMLElement;

        const btn = target instanceof HTMLElement && target.closest("button[data-action]") as HTMLElement | null;
        if (btn) {
          event.stopPropagation();
          const action = (btn as HTMLElement).dataset.action;
          const repoId = (btn as HTMLElement).dataset.repo;
          if (action && repoId) {
            handleRepoAction(repoId, action);
          }
          return;
        }

        const card = target instanceof HTMLElement && target.closest(".hub-repo-clickable") as HTMLElement | null;
        if (card && card.dataset.href) {
          window.location.href = card.dataset.href;
        }
      });

    repoListEl.addEventListener("keydown", (event) => {
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

    repoListEl.addEventListener("mouseover", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const card = target.closest(".hub-repo-clickable") as HTMLElement | null;
      if (card && card.dataset.href) {
        prefetchRepo(card.dataset.href);
      }
    });

    repoListEl.addEventListener("pointerdown", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const card = target.closest(".hub-repo-clickable") as HTMLElement | null;
      if (card && card.dataset.href) {
        prefetchRepo(card.dataset.href);
      }
    });
  }
}

async function silentRefreshHub(): Promise<void> {
  try {
    const data = await api("/hub/repos", { method: "GET" }) as HubData;
    hubData = data;
    markHubRefreshed();
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(data.repos || []);
    renderReposWithScroll(data.repos || []);
    await loadHubUsage({ silent: true, allowRetry: false });
  } catch (err) {
    console.error("Auto-refresh hub failed:", err);
  }
}

function markHubRefreshed(): void {
  lastHubAutoRefreshAt = Date.now();
}

function hasActiveRuns(repos: HubRepo[]): boolean {
  return repos.some((repo) => repo.status === "running");
}

async function dynamicRefreshHub(): Promise<void> {
  const now = Date.now();
  const running = hasActiveRuns(hubData.repos || []);
  const minInterval = running ? HUB_REFRESH_ACTIVE_MS : HUB_REFRESH_IDLE_MS;
  if (now - lastHubAutoRefreshAt < minInterval) return;
  await silentRefreshHub();
}

async function loadHubVersion(): Promise<void> {
  if (!hubVersionEl) return;
  try {
    const data = await api("/hub/version", { method: "GET" });
    const version = (data as { asset_version?: string }).asset_version || "";
    hubVersionEl.textContent = version ? `v${version}` : "v–";
  } catch (_err) {
    hubVersionEl.textContent = "v–";
  }
}

async function checkUpdateStatus(): Promise<void> {
  try {
    const data = await api("/system/update/status", { method: "GET" });
    if (!data || !(data as { status?: string }).status) return;
    const stamp = (data as { at?: string | number }).at ? String((data as { at?: string | number }).at) : "";
    if (stamp && sessionStorage.getItem(UPDATE_STATUS_SEEN_KEY) === stamp) return;
    if ((data as { status?: string }).status === "rollback" || (data as { status?: string }).status === "error") {
      flash((data as { message?: string }).message || "Update failed; rollback attempted.", "error");
    }
    if (stamp) sessionStorage.setItem(UPDATE_STATUS_SEEN_KEY, stamp);
  } catch (_err) {
    // Ignore update status failures; UI still renders.
  }
}

function prefetchRepo(url: string): void {
  if (!url || prefetchedUrls.has(url)) return;
  prefetchedUrls.add(url);
  fetch(url, { method: "GET", headers: { "x-prefetch": "1" } }).catch(() => {});
}

export function initHub(): void {
  if (!repoListEl) return;
  attachHubHandlers();
  initHubUsageChartControls();
  hubInboxRefresh?.addEventListener("click", () => {
    void loadHubInbox({ reason: "manual" });
  });
  const cachedHub = loadSessionCache<HubData | null>(HUB_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedHub) {
    hubData = cachedHub;
    renderSummary(cachedHub.repos || []);
    renderReposWithScroll(cachedHub.repos || []);
  }
  const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedUsage) {
    renderHubUsage(cachedUsage);
  }
  loadHubUsageSeries();
  refreshHub();
  loadHubVersion();
  checkUpdateStatus();

  registerAutoRefresh("hub-repos", {
    callback: async (ctx) => {
      void ctx;
      await dynamicRefreshHub();
    },
    tabId: null,
    interval: HUB_REFRESH_ACTIVE_MS,
    refreshOnActivation: true,
    immediate: false,
  });
}

export const __hubTest = {
  renderRepos,
  renderHubUsage,
};
