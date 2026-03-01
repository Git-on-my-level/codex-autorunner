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
import { registerAutoRefresh } from "./autoRefresh.js";
import { HUB_BASE } from "./env.js";
import { preserveScroll } from "./preserve.js";
import { initNotificationBell } from "./notificationBell.js";

interface HubTicketFlow {
  status: string;
  done_count: number;
  total_count: number;
  current_step: number | null;
  failure?: Record<string, unknown> | null;
  failure_summary?: string | null;
}

interface HubTicketFlowDisplay {
  status: string;
  status_label: string;
  status_icon: string;
  is_active: boolean;
  done_count: number;
  total_count: number;
  run_id: string | null;
}

interface HubRepo {
  id: string;
  path: string;
  display_name: string;
  enabled: boolean;
  auto_run: boolean;
  worktree_setup_commands?: string[] | null;
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
  effective_destination: Record<string, unknown>;
  mounted: boolean;
  mount_error?: string | null;
  chat_bound?: boolean;
  chat_bound_thread_count?: number | null;
  ticket_flow?: HubTicketFlow | null;
  ticket_flow_display?: HubTicketFlowDisplay | null;
}

interface HubData {
  repos: HubRepo[];
  last_scan_at: string | null;
  pinned_parent_repo_ids?: string[];
}

interface HubDestinationResponse {
  repo_id: string;
  configured_destination: Record<string, unknown> | null;
  effective_destination: Record<string, unknown>;
  source: string;
  issues?: string[];
}

interface HubChannelEntry {
  key: string;
  display?: string | null;
  seen_at?: string | null;
  meta?: Record<string, unknown> | null;
  entry?: Record<string, unknown> | null;
}

interface HubChannelDirectoryResponse {
  entries: HubChannelEntry[];
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

type HubFlowFilter =
  | "all"
  | "active"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "idle";
type HubSortOrder =
  | "repo_id"
  | "last_activity_desc"
  | "last_activity_asc"
  | "flow_progress_desc";

interface HubViewPrefs {
  flowFilter: HubFlowFilter;
  sortOrder: HubSortOrder;
}

interface HubRepoGroup {
  base: HubRepo;
  worktrees: HubRepo[];
  filteredWorktrees: HubRepo[];
  matchesFilter: boolean;
  pinned: boolean;
  lastActivityMs: number;
  flowProgress: number;
}

function isChatBoundWorktree(repo: HubRepo): boolean {
  if ((repo.kind || "base") !== "worktree") return false;
  const boundCount = Number(repo.chat_bound_thread_count || 0);
  return repo.chat_bound === true || boundCount > 0;
}

const HUB_VIEW_PREFS_KEY = `car:hub-view-prefs:${HUB_BASE || "/"}`;
const HUB_DEFAULT_VIEW_PREFS: HubViewPrefs = {
  flowFilter: "all",
  sortOrder: "repo_id",
};

let hubData: HubData = {
  repos: [],
  last_scan_at: null,
  pinned_parent_repo_ids: [],
};
const prefetchedUrls = new Set<string>();
const hubViewPrefs: HubViewPrefs = { ...HUB_DEFAULT_VIEW_PREFS };
let pinnedParentRepoIds = new Set<string>();

const HUB_CACHE_TTL_MS = 30000;
const HUB_CACHE_KEY = `car:hub:${HUB_BASE || "/"}`;
const HUB_USAGE_CACHE_KEY = `car:hub-usage:${HUB_BASE || "/"}`;
const HUB_REFRESH_ACTIVE_MS = 5000;
const HUB_REFRESH_IDLE_MS = 30000;

let lastHubAutoRefreshAt = 0;

const repoListEl = document.getElementById("hub-repo-list");
const lastScanEl = document.getElementById("hub-last-scan");
const pmaLastScanEl = document.getElementById("pma-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");
const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
const hubUsageChartCanvas = document.getElementById("hub-usage-chart-canvas");
const hubUsageChartRange = document.getElementById("hub-usage-chart-range");
const hubUsageChartSegment = document.getElementById("hub-usage-chart-segment");
const hubVersionEl = document.getElementById("hub-version");
const pmaVersionEl = document.getElementById("pma-version");
const hubChannelQueryInput = document.getElementById(
  "hub-channel-query"
) as HTMLInputElement | null;
const hubChannelLimitInput = document.getElementById(
  "hub-channel-limit"
) as HTMLInputElement | null;
const hubChannelSearchBtn = document.getElementById(
  "hub-channel-search"
) as HTMLButtonElement | null;
const hubChannelRefreshBtn = document.getElementById(
  "hub-channel-refresh"
) as HTMLButtonElement | null;
const hubChannelListEl = document.getElementById("hub-channel-list");
const hubFlowFilterEl = document.getElementById(
  "hub-flow-filter"
) as HTMLSelectElement | null;
const hubSortOrderEl = document.getElementById(
  "hub-sort-order"
) as HTMLSelectElement | null;
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
let hubUsageIndex: Record<string, HubUsageRepo> = {};
let hubUsageUnmatched: HubUsageData["unmatched"] | null = null;

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

function saveHubViewPrefs(): void {
  try {
    localStorage.setItem(HUB_VIEW_PREFS_KEY, JSON.stringify(hubViewPrefs));
  } catch (_err) {
    // Ignore local storage failures; prefs are best-effort.
  }
}

function loadHubViewPrefs(): void {
  try {
    const raw = localStorage.getItem(HUB_VIEW_PREFS_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as Partial<HubViewPrefs>;
    const flowFilter = parsed.flowFilter;
    const sortOrder = parsed.sortOrder;
    if (
      flowFilter === "all" ||
      flowFilter === "active" ||
      flowFilter === "running" ||
      flowFilter === "paused" ||
      flowFilter === "completed" ||
      flowFilter === "failed" ||
      flowFilter === "idle"
    ) {
      hubViewPrefs.flowFilter = flowFilter;
    }
    if (
      sortOrder === "repo_id" ||
      sortOrder === "last_activity_desc" ||
      sortOrder === "last_activity_asc" ||
      sortOrder === "flow_progress_desc"
    ) {
      hubViewPrefs.sortOrder = sortOrder;
    }
  } catch (_err) {
    // Ignore parse/storage errors; defaults apply.
  }
}

function normalizePinnedParentRepoIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  value.forEach((entry) => {
    if (typeof entry !== "string") return;
    const repoId = entry.trim();
    if (!repoId || seen.has(repoId)) return;
    seen.add(repoId);
    out.push(repoId);
  });
  return out;
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

function formatDestinationSummary(
  destination: Record<string, unknown> | null | undefined
): string {
  if (!destination || typeof destination !== "object") return "local";
  const kindRaw = destination.kind;
  const kind = typeof kindRaw === "string" ? kindRaw.trim().toLowerCase() : "local";
  if (kind === "docker") {
    const image = typeof destination.image === "string" ? destination.image.trim() : "";
    return image ? `docker:${image}` : "docker";
  }
  return "local";
}

function splitCommaSeparated(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function currentDockerEnvPassthrough(
  destination: Record<string, unknown> | null | undefined
): string {
  const raw = destination?.env_passthrough;
  if (!Array.isArray(raw)) return "";
  return raw
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .join(", ");
}

function currentDockerMounts(
  destination: Record<string, unknown> | null | undefined
): string {
  const raw = destination?.mounts;
  if (!Array.isArray(raw)) return "";
  const mounts = raw
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const source = String((item as Record<string, unknown>).source || "").trim();
      const target = String((item as Record<string, unknown>).target || "").trim();
      return source && target ? `${source}:${target}` : "";
    })
    .filter(Boolean);
  return mounts.join(", ");
}

function parseDockerMountList(
  value: string
): { mounts: Array<{ source: string; target: string }>; error: string | null } {
  const mounts: Array<{ source: string; target: string }> = [];
  const entries = splitCommaSeparated(value);
  for (const entry of entries) {
    const splitAt = entry.lastIndexOf(":");
    if (splitAt <= 0 || splitAt >= entry.length - 1) {
      return {
        mounts: [],
        error: `Invalid mount "${entry}". Use source:target (comma-separated).`,
      };
    }
    const source = entry.slice(0, splitAt).trim();
    const target = entry.slice(splitAt + 1).trim();
    if (!source || !target) {
      return {
        mounts: [],
        error: `Invalid mount "${entry}". Use source:target (comma-separated).`,
      };
    }
    mounts.push({ source, target });
  }
  return { mounts, error: null };
}

function resolveHubChannelLimit(): number {
  const raw = (hubChannelLimitInput?.value || "").trim();
  if (!raw) return 100;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error("Channel directory limit must be a positive integer.");
  }
  if (parsed > 1000) {
    throw new Error("Channel directory limit must be <= 1000.");
  }
  return parsed;
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
  if (pmaLastScanEl) {
    pmaLastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
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

function getRepoUsage(repoId: string): { label: string; hasData: boolean } {
  const usage = hubUsageIndex[repoId];
  if (!usage) return { label: "—", hasData: false };
  const totals = usage.totals || {};
  return {
    label: formatTokensCompact(totals.total_tokens),
    hasData: true,
  };
}

function indexHubUsage(data: HubUsageData | null): void {
  hubUsageIndex = {};
  hubUsageUnmatched = data?.unmatched || null;
  if (!data?.repos) return;
  data.repos.forEach((repo) => {
    if (repo?.id) hubUsageIndex[repo.id] = repo;
  });
}

function renderHubUsageMeta(data: HubUsageData | null): void {
  if (hubUsageMeta) {
    hubUsageMeta.textContent = data?.codex_home || "–";
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
  const effective = hasSummary ? data : cachedUsage;

  if (effective) {
    indexHubUsage(effective);
    renderHubUsageMeta(effective);
    renderReposWithScroll(hubData.repos || []);
  }

  if (data?.status === "loading") {
    if (allowRetry) scheduleHubUsageSummaryRetry();
    return Boolean(hasSummary);
  }

  if (hasSummary) {
    clearHubUsageSummaryRetry();
    return true;
  }

  if (!effective && !data) {
    renderReposWithScroll(hubData.repos || []);
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
      handleHubUsagePayload(cachedUsage, { cachedUsage, allowRetry: false });
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
  both: "Web + Chat Apps",
  web: "web only",
  chat: "Chat Apps (Telegram + Discord)",
  telegram: "Telegram only",
  discord: "Discord only",
};

type UpdateTarget = "both" | "web" | "chat" | "telegram" | "discord";

function normalizeUpdateTarget(value: unknown): UpdateTarget {
  if (!value) return "both";
  if (
    value === "both" ||
    value === "web" ||
    value === "chat" ||
    value === "telegram" ||
    value === "discord"
  ) {
    return value as UpdateTarget;
  }
  return "both";
}

function getUpdateTarget(selectId: string | null): UpdateTarget {
  const select = selectId ? (document.getElementById(selectId) as HTMLSelectElement | null) : null;
  return normalizeUpdateTarget(select ? select.value : "both");
}

function describeUpdateTarget(target: UpdateTarget): string {
  return UPDATE_TARGET_LABELS[target] || UPDATE_TARGET_LABELS.both;
}

function includesWebUpdateTarget(target: UpdateTarget): boolean {
  return target === "both" || target === "web";
}

function updateRestartNotice(target: UpdateTarget): string {
  if (target === "chat") return "Telegram and Discord bots will restart.";
  if (target === "telegram") return "The Telegram bot will restart.";
  if (target === "discord") return "The Discord bot will restart.";
  return "The service will restart.";
}

interface UpdateTargetOptionResponse {
  value?: string;
  label?: string;
}

interface UpdateTargetsResponse {
  targets?: UpdateTargetOptionResponse[];
  default_target?: string;
}

async function loadUpdateTargetOptions(selectId: string | null): Promise<void> {
  const select = selectId ? (document.getElementById(selectId) as HTMLSelectElement | null) : null;
  if (!select) return;
  const isInitialized = select.dataset.updateTargetsInitialized === "1";
  let payload: UpdateTargetsResponse | null = null;
  try {
    payload = await api("/system/update/targets", { method: "GET" }) as UpdateTargetsResponse;
  } catch (_err) {
    return;
  }
  const rawOptions = Array.isArray(payload?.targets) ? payload.targets : [];
  const options: Array<{ value: UpdateTarget; label: string }> = [];
  const seen = new Set<string>();
  rawOptions.forEach((entry) => {
    const rawValue = typeof entry?.value === "string" ? entry.value : "";
    if (!["both", "web", "chat", "telegram", "discord"].includes(rawValue)) return;
    if (!rawValue) return;
    const value = normalizeUpdateTarget(rawValue);
    if (seen.has(value)) return;
    seen.add(value);
    const label = typeof entry?.label === "string" && entry.label.trim()
      ? entry.label.trim()
      : describeUpdateTarget(value);
    options.push({ value, label });
  });
  if (!options.length) return;

  const previous = normalizeUpdateTarget(select.value || "both");
  const hasPrevious = options.some((item) => item.value === previous);
  const defaultTarget = normalizeUpdateTarget(payload?.default_target || "both");
  const fallback = options.some((item) => item.value === defaultTarget)
    ? defaultTarget
    : options[0].value;

  select.replaceChildren();
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.appendChild(option);
  });
  if (isInitialized) {
    select.value = hasPrevious ? previous : fallback;
  } else {
    select.value = fallback;
    select.dataset.updateTargetsInitialized = "1";
  }
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

  const restartNotice = updateRestartNotice(updateTarget);
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
    if (!includesWebUpdateTarget(updateTarget)) {
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
  const settingsBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>("#hub-settings, #pma-settings")
  );
  const modal = document.getElementById("hub-settings-modal");
  const closeBtn = document.getElementById("hub-settings-close");
  const updateBtn = document.getElementById("hub-update-btn") as HTMLButtonElement | null;
  const updateTarget = document.getElementById("hub-update-target") as HTMLSelectElement | null;
  void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
  let closeModal: (() => void) | null = null;

  const hideModal = () => {
    if (closeModal) {
      const close = closeModal;
      closeModal = null;
      close();
    }
  };

  if (modal && settingsBtns.length > 0) {
    settingsBtns.forEach((settingsBtn) => {
      settingsBtn.addEventListener("click", () => {
        const triggerEl = document.activeElement;
        hideModal();
        closeModal = openModal(modal, {
          initialFocus: closeBtn || updateBtn || modal,
          returnFocusTo: triggerEl as HTMLElement | null,
          onRequestClose: hideModal,
        });
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
    actions.push({
      key: "repo_settings",
      label: "Settings",
      kind: "ghost",
      title: "Repository settings",
    });
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
  return actions;
}

async function openRepoSettingsModal(repo: HubRepo): Promise<void> {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.hidden = true;

  const dialog = document.createElement("div");
  dialog.className = "modal-dialog repo-settings-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");

  const header = document.createElement("div");
  header.className = "modal-header";
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = `Settings: ${repo.display_name || repo.id}`;
  header.appendChild(title);

  const body = document.createElement("div");
  body.className = "modal-body";

  const worktreeSection = document.createElement("div");
  worktreeSection.className = "form-group";
  const worktreeLabel = document.createElement("label");
  worktreeLabel.textContent = "Worktree Setup Commands";
  const worktreeHint = document.createElement("p");
  worktreeHint.className = "muted small";
  worktreeHint.textContent =
    "Commands run with /bin/sh -lc after creating a new worktree. One per line, leave blank to disable.";
  const textarea = document.createElement("textarea");
  textarea.rows = 6;
  textarea.style.width = "100%";
  textarea.style.resize = "vertical";
  textarea.placeholder = "make setup\npnpm install\npre-commit install";
  textarea.value = (repo.worktree_setup_commands || []).join("\n");
  worktreeSection.append(worktreeLabel, worktreeHint, textarea);
  body.appendChild(worktreeSection);

  const destinationSection = document.createElement("div");
  destinationSection.className = "form-group";
  const destinationLabel = document.createElement("label");
  destinationLabel.textContent = "Execution Destination";
  const destinationHint = document.createElement("p");
  destinationHint.className = "muted small";
  destinationHint.textContent = "Set where runs execute for this repo.";
  const destinationRow = document.createElement("div");
  destinationRow.className = "settings-actions";
  const destinationPill = document.createElement("span");
  destinationPill.className = "pill pill-small hub-destination-settings-pill";
  destinationPill.textContent = formatDestinationSummary(repo.effective_destination);
  const destinationBtn = document.createElement("button");
  destinationBtn.className = "ghost";
  destinationBtn.textContent = "Change destination";
  destinationRow.append(destinationPill, destinationBtn);
  destinationSection.append(destinationLabel, destinationHint, destinationRow);
  body.appendChild(destinationSection);

  const dangerSection = document.createElement("div");
  dangerSection.className = "form-group";
  const dangerLabel = document.createElement("label");
  dangerLabel.textContent = "Danger Zone";
  const dangerHint = document.createElement("p");
  dangerHint.className = "muted small";
  dangerHint.textContent =
    "Remove this repo from hub and delete its local directory.";
  const dangerRow = document.createElement("div");
  dangerRow.className = "settings-actions";
  const removeBtn = document.createElement("button");
  removeBtn.className = "danger";
  removeBtn.textContent = "Remove repo";
  dangerRow.append(removeBtn);
  dangerSection.append(dangerLabel, dangerHint, dangerRow);
  body.appendChild(dangerSection);

  const footer = document.createElement("div");
  footer.className = "modal-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost";
  cancelBtn.textContent = "Cancel";
  const saveBtn = document.createElement("button");
  saveBtn.className = "primary";
  saveBtn.textContent = "Save";
  footer.append(cancelBtn, saveBtn);

  dialog.append(header, body, footer);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  return new Promise((resolve) => {
    let closeModal: (() => void) | null = null;
    let settled = false;

    const finalize = async (
      action: "cancel" | "save" | "destination" | "remove"
    ) => {
      if (settled) return;
      settled = true;
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      overlay.remove();

      if (action === "save") {
        const commands = textarea.value
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
        try {
          await api(`/hub/repos/${encodeURIComponent(repo.id)}/worktree-setup`, {
            method: "POST",
            body: { commands },
          });
          flash(
            commands.length
              ? `Saved ${commands.length} setup command(s) for ${repo.id}`
              : `Cleared setup commands for ${repo.id}`,
            "success"
          );
          await refreshHub();
        } catch (err) {
          flash(
            (err as Error).message || "Failed to save settings",
            "error"
          );
        }
      }
      if (action === "destination") {
        const updated = await promptAndSetRepoDestination(repo);
        if (updated) {
          await refreshHub();
        }
      }
      if (action === "remove") {
        await removeRepoWithChecks(repo.id);
      }
      resolve();
    };

    closeModal = openModal(overlay, {
      initialFocus: textarea,
      returnFocusTo: document.activeElement as HTMLElement | null,
      onRequestClose: () => finalize("cancel"),
      onKeydown: (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          finalize("save");
        }
      },
    });

    cancelBtn.addEventListener("click", () => finalize("cancel"));
    saveBtn.addEventListener("click", () => finalize("save"));
    destinationBtn.addEventListener("click", () => finalize("destination"));
    removeBtn.addEventListener("click", () => finalize("remove"));
  });
}

function buildDestinationBadge(
  destination: Record<string, unknown> | null | undefined
): string {
  const summary = formatDestinationSummary(destination);
  const isDocker = summary.startsWith("docker");
  const label = isDocker ? "docker" : "local";
  const titleAttr =
    summary !== label ? ` title="${escapeHtml(summary)}"` : "";
  const className = isDocker
    ? "pill pill-small pill-info hub-destination-pill hub-destination-pill-docker"
    : "pill pill-small pill-info hub-destination-pill";
  return `<span class="${className}"${titleAttr}>${escapeHtml(label)}</span>`;
}

function buildFlowStatusBadge(statusLabel: string, statusValue: string): string {
  const normalized = String(statusValue || "idle").toLowerCase();
  if (["idle", "completed", "success", "ready"].includes(normalized)) {
    return "";
  }
  return `<span class="pill pill-small hub-status-pill">${escapeHtml(
    statusLabel
  )}</span>`;
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
  } else if (repo.mounted !== true) {
    label = "not mounted";
    className += " pill-warn";
  } else {
    return "";
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

function repoLastActivityMs(repo: HubRepo): number {
  const raw = repo.last_run_finished_at || repo.last_run_started_at;
  if (!raw) return 0;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function repoFlowStatus(repo: HubRepo): string {
  const status = repo.ticket_flow_display?.status || repo.ticket_flow?.status || "idle";
  return String(status || "idle").toLowerCase();
}

function repoFlowProgress(repo: HubRepo): number {
  const done = Number(repo.ticket_flow_display?.done_count || repo.ticket_flow?.done_count || 0);
  const total = Number(repo.ticket_flow_display?.total_count || repo.ticket_flow?.total_count || 0);
  if (total <= 0) return 0;
  return done / total;
}

function repoMatchesFlowFilter(repo: HubRepo, filter: HubFlowFilter): boolean {
  if (filter === "all") return true;
  const flowStatus = repoFlowStatus(repo);
  if (filter === "active") {
    return (
      flowStatus === "running" ||
      flowStatus === "pending" ||
      flowStatus === "paused" ||
      flowStatus === "stopping"
    );
  }
  if (filter === "running") return flowStatus === "running";
  if (filter === "paused") return flowStatus === "paused";
  if (filter === "completed") return flowStatus === "completed" || flowStatus === "done";
  if (filter === "failed") {
    return (
      flowStatus === "failed" ||
      flowStatus === "stopped" ||
      flowStatus === "superseded"
    );
  }
  return flowStatus === "idle";
}

function compareReposForSort(a: HubRepo, b: HubRepo, sortOrder: HubSortOrder): number {
  if (sortOrder === "last_activity_desc") {
    return (
      repoLastActivityMs(b) - repoLastActivityMs(a) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  if (sortOrder === "last_activity_asc") {
    return (
      repoLastActivityMs(a) - repoLastActivityMs(b) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  if (sortOrder === "flow_progress_desc") {
    return (
      repoFlowProgress(b) - repoFlowProgress(a) ||
      repoLastActivityMs(b) - repoLastActivityMs(a) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  return String(a.id).localeCompare(String(b.id));
}

function buildRepoGroups(repos: HubRepo[]): {
  groups: HubRepoGroup[];
  orphanWorktrees: HubRepo[];
  chatBoundWorktrees: HubRepo[];
} {
  const bases = repos.filter((r) => (r.kind || "base") === "base");
  const allWorktrees = repos.filter((r) => (r.kind || "base") === "worktree");
  const chatBoundWorktrees: HubRepo[] = [];
  const worktrees: HubRepo[] = [];
  allWorktrees.forEach((repo) => {
    if (isChatBoundWorktree(repo)) {
      chatBoundWorktrees.push(repo);
      return;
    }
    worktrees.push(repo);
  });
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

  const groups: HubRepoGroup[] = [...byBase.values()].map((group) => {
    const filteredWorktrees =
      hubViewPrefs.flowFilter === "all"
        ? [...group.worktrees]
        : group.worktrees.filter((repo) =>
            repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter)
          );
    const baseMatches = repoMatchesFlowFilter(group.base, hubViewPrefs.flowFilter);
    const matchesFilter =
      hubViewPrefs.flowFilter === "all" || baseMatches || filteredWorktrees.length > 0;
    const combined = [group.base, ...group.worktrees];
    const lastActivityMs = combined.reduce((latest, repo) => {
      return Math.max(latest, repoLastActivityMs(repo));
    }, 0);
    const flowProgress = combined.reduce((best, repo) => {
      return Math.max(best, repoFlowProgress(repo));
    }, 0);
    return {
      base: group.base,
      worktrees: [...group.worktrees],
      filteredWorktrees,
      matchesFilter,
      pinned: pinnedParentRepoIds.has(group.base.id),
      lastActivityMs,
      flowProgress,
    };
  });

  return { groups, orphanWorktrees, chatBoundWorktrees };
}

function renderRepos(repos: HubRepo[]): void {
  if (!repoListEl) return;
  repoListEl.innerHTML = "";
  if (!repos.length) {
    repoListEl.innerHTML =
      '<div class="hub-empty muted">No repos discovered yet. Run a scan or create a new repo.</div>';
    return;
  }

  const { groups, orphanWorktrees, chatBoundWorktrees } = buildRepoGroups(repos);
  const orderedGroups = groups
    .filter((group) => group.matchesFilter)
    .sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      if (hubViewPrefs.sortOrder === "last_activity_desc") {
        return (
          b.lastActivityMs - a.lastActivityMs ||
          String(a.base.id).localeCompare(String(b.base.id))
        );
      }
      if (hubViewPrefs.sortOrder === "last_activity_asc") {
        return (
          a.lastActivityMs - b.lastActivityMs ||
          String(a.base.id).localeCompare(String(b.base.id))
        );
      }
      if (hubViewPrefs.sortOrder === "flow_progress_desc") {
        return (
          b.flowProgress - a.flowProgress ||
          b.lastActivityMs - a.lastActivityMs ||
          String(a.base.id).localeCompare(String(b.base.id))
        );
      }
      return String(a.base.id).localeCompare(String(b.base.id));
    });

  const filteredOrphans =
    hubViewPrefs.flowFilter === "all"
      ? [...orphanWorktrees]
      : orphanWorktrees.filter((repo) =>
          repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter)
        );
  filteredOrphans.sort((a, b) => compareReposForSort(a, b, hubViewPrefs.sortOrder));
  const filteredChatBound =
    hubViewPrefs.flowFilter === "all"
      ? [...chatBoundWorktrees]
      : chatBoundWorktrees.filter((repo) =>
          repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter)
        );
  filteredChatBound.sort((a, b) =>
    compareReposForSort(a, b, hubViewPrefs.sortOrder)
  );

  if (!orderedGroups.length && !filteredOrphans.length && !filteredChatBound.length) {
    repoListEl.innerHTML =
      '<div class="hub-empty muted">No repos match the selected flow filter.</div>';
    return;
  }

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
    const isPinnedParent = !isWorktreeRow && repo.kind === "base" && pinnedParentRepoIds.has(repo.id);
    const pinAction = !isWorktreeRow && repo.kind === "base"
      ? `<button class="ghost sm icon-btn hub-pin-btn${isPinnedParent ? " active" : ""}" data-action="${
          isPinnedParent ? "unpin_parent" : "pin_parent"
        }" data-repo="${escapeHtml(repo.id)}" title="${
          isPinnedParent ? "Unpin parent repo" : "Pin parent repo"
        }" aria-label="${
          isPinnedParent ? "Unpin parent repo" : "Pin parent repo"
        }"><span class="hub-pin-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6"></path><path d="M12 14v7"></path></svg></span></button>`
      : "";

    const flowDisplay = repo.ticket_flow_display;
    const statusText = flowDisplay?.status_label || repo.status;
    const statusValue = flowDisplay?.status || repo.status;
    const statusBadge = buildFlowStatusBadge(statusText, statusValue);
    const mountBadge = buildMountBadge(repo);
    const destinationBadge = buildDestinationBadge(repo.effective_destination);
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
    const infoSubline = infoLine
      ? `<div class="hub-repo-subline">${infoLine}</div>`
      : "";

    const setupBadge =
      (repo.worktree_setup_commands || []).length > 0 && repo.kind === "base"
        ? '<span class="pill pill-small pill-success">setup</span>'
        : "";
    const metadataBadges = [
      destinationBadge,
      statusBadge,
      mountBadge,
      lockBadge,
      initBadge,
      setupBadge,
    ]
      .filter(Boolean)
      .join("");

    const usageInfo = getRepoUsage(repo.id);
    const usageBadge = `<span class="pill pill-small hub-usage-pill${
      usageInfo.hasData ? "" : " muted"
    }">${escapeHtml(usageInfo.label)}</span>`;

    // Ticket flow progress line
    let ticketFlowLine = "";
    const tf = repo.ticket_flow;
    if (flowDisplay && flowDisplay.total_count > 0) {
      const percent = Math.round(
        (flowDisplay.done_count / flowDisplay.total_count) * 100
      );
      const isActive = Boolean(flowDisplay.is_active);
      const currentStep = tf?.current_step;
      const statusSuffix =
        flowDisplay.status === "paused"
          ? " · paused"
          : currentStep
          ? ` · step ${currentStep}`
          : "";
      ticketFlowLine = `
        <div class="hub-repo-flow-line${isActive ? " active" : ""}">
          <div class="hub-flow-bar">
            <div class="hub-flow-fill" style="width:${percent}%"></div>
          </div>
          <span class="hub-flow-text">${escapeHtml(
            flowDisplay.status_label
          )} ${flowDisplay.done_count}/${flowDisplay.total_count}${statusSuffix}</span>
        </div>`;
    }

    card.innerHTML = `
      <div class="hub-repo-row">
        ${pinAction ? `<div class="hub-repo-left">${pinAction}</div>` : ""}
        <div class="hub-repo-center">
          <div class="hub-repo-mainline">
            <span class="hub-repo-title">${escapeHtml(
              repo.display_name
            )}</span>
            <div class="hub-repo-meta-inline">${metadataBadges}</div>
            ${usageBadge}
          </div>
          ${infoSubline}
          ${ticketFlowLine}
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
      statusPill(statusEl, flowDisplay?.status || repo.status);
    }

    repoListEl.appendChild(card);
  };

  orderedGroups.forEach((group) => {
    const repo = group.base;
    renderRepoCard(repo, { isWorktreeRow: false });
    const worktrees = [...group.filteredWorktrees].sort((a, b) =>
      compareReposForSort(a, b, hubViewPrefs.sortOrder)
    );
    if (worktrees.length) {
      const list = document.createElement("div");
      list.className = "hub-worktree-list";
      worktrees.forEach((wt) => {
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

  if (filteredOrphans.length) {
    const header = document.createElement("div");
    header.className = "hub-worktree-orphans muted small";
    header.textContent = "Orphan worktrees";
    repoListEl.appendChild(header);
    filteredOrphans.forEach((wt) => renderRepoCard(wt, { isWorktreeRow: true }));
  }
  if (filteredChatBound.length) {
    const header = document.createElement("div");
    header.className = "hub-worktree-orphans muted small";
    header.textContent = "Chat bound worktrees";
    repoListEl.appendChild(header);
    filteredChatBound.forEach((wt) =>
      renderRepoCard(wt, { isWorktreeRow: true })
    );
  }

  if (hubUsageUnmatched && hubUsageUnmatched.events) {
    const note = document.createElement("div");
    note.className = "hub-usage-unmatched-note muted small";
    const total = formatTokensCompact(hubUsageUnmatched.totals?.total_tokens);
    note.textContent = `Other: ${total} · ${hubUsageUnmatched.events}ev (unattributed)`;
    repoListEl.appendChild(note);
  }
}

function renderReposWithScroll(repos: HubRepo[]): void {
  preserveScroll(repoListEl, () => {
    renderRepos(repos);
  }, { restoreOnNextFrame: true });
}

function applyHubData(data: HubData): void {
  hubData = {
    repos: Array.isArray(data?.repos) ? data.repos : [],
    last_scan_at: data?.last_scan_at || null,
    pinned_parent_repo_ids: normalizePinnedParentRepoIds(
      data?.pinned_parent_repo_ids
    ),
  };
  pinnedParentRepoIds = new Set(
    normalizePinnedParentRepoIds(hubData.pinned_parent_repo_ids)
  );
}

async function refreshHub(): Promise<void> {
  setButtonLoading(true);
  try {
    const data = await api("/hub/repos", { method: "GET" }) as HubData;
    applyHubData(data);
    markHubRefreshed();
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(hubData.repos || []);
    renderReposWithScroll(hubData.repos || []);
    loadHubUsage({ silent: true }).catch(() => {});
    loadHubChannelDirectory({ silent: true }).catch(() => {});
  } catch (err) {
    flash((err as Error).message || "Hub request failed", "error");
  } finally {
    setButtonLoading(false);
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

function initHubRepoListControls(): void {
  loadHubViewPrefs();
  if (hubFlowFilterEl) {
    hubFlowFilterEl.value = hubViewPrefs.flowFilter;
    hubFlowFilterEl.addEventListener("change", () => {
      hubViewPrefs.flowFilter = hubFlowFilterEl.value as HubFlowFilter;
      saveHubViewPrefs();
      renderReposWithScroll(hubData.repos || []);
    });
  }
  if (hubSortOrderEl) {
    hubSortOrderEl.value = hubViewPrefs.sortOrder;
    hubSortOrderEl.addEventListener("change", () => {
      hubViewPrefs.sortOrder = hubSortOrderEl.value as HubSortOrder;
      saveHubViewPrefs();
      renderReposWithScroll(hubData.repos || []);
    });
  }
}

async function setParentRepoPinned(repoId: string, pinned: boolean): Promise<void> {
  const response = await api(`/hub/repos/${encodeURIComponent(repoId)}/pin`, {
    method: "POST",
    body: { pinned },
  }) as { pinned_parent_repo_ids?: unknown };
  pinnedParentRepoIds = new Set(
    normalizePinnedParentRepoIds(response?.pinned_parent_repo_ids)
  );
  hubData.pinned_parent_repo_ids = Array.from(pinnedParentRepoIds);
}

type DestinationKind = "local" | "docker";

async function chooseDestinationKind(
  repo: HubRepo,
  currentKind: DestinationKind
): Promise<DestinationKind | null> {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.hidden = true;

  const dialog = document.createElement("div");
  dialog.className = "modal-dialog repo-settings-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");

  const header = document.createElement("div");
  header.className = "modal-header";
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = `Set destination: ${repo.display_name || repo.id}`;
  header.appendChild(title);

  const body = document.createElement("div");
  body.className = "modal-body";
  const hint = document.createElement("p");
  hint.className = "muted small";
  hint.textContent = "Choose execution destination kind.";
  body.appendChild(hint);

  const footer = document.createElement("div");
  footer.className = "modal-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost";
  cancelBtn.textContent = "Cancel";

  const localBtn = document.createElement("button");
  localBtn.className = currentKind === "local" ? "primary" : "ghost";
  localBtn.textContent = "Local";

  const dockerBtn = document.createElement("button");
  dockerBtn.className = currentKind === "docker" ? "primary" : "ghost";
  dockerBtn.textContent = "Docker";

  footer.append(cancelBtn, localBtn, dockerBtn);
  dialog.append(header, body, footer);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  return new Promise((resolve) => {
    let closeModal: (() => void) | null = null;
    let settled = false;
    const returnFocusTo = document.activeElement as HTMLElement | null;

    const finalize = (selected: DestinationKind | null) => {
      if (settled) return;
      settled = true;
      if (closeModal) {
        const close = closeModal;
        closeModal = null;
        close();
      }
      overlay.remove();
      resolve(selected);
    };

    closeModal = openModal(overlay, {
      initialFocus: currentKind === "docker" ? dockerBtn : localBtn,
      returnFocusTo,
      onRequestClose: () => finalize(null),
    });

    cancelBtn.addEventListener("click", () => finalize(null));
    localBtn.addEventListener("click", () => finalize("local"));
    dockerBtn.addEventListener("click", () => finalize("docker"));
  });
}

async function promptAndSetRepoDestination(repo: HubRepo): Promise<boolean> {
  const current = formatDestinationSummary(repo.effective_destination);
  const currentKind =
    current.startsWith("docker:") || current === "docker" ? "docker" : "local";
  const kind = await chooseDestinationKind(repo, currentKind);
  if (!kind) return false;
  const body: Record<string, unknown> = { kind };
  if (kind === "docker") {
    const currentImage =
      typeof repo.effective_destination?.image === "string"
        ? String(repo.effective_destination.image)
        : "";
    const imageValue = await inputModal("Docker image:", {
      placeholder: "ghcr.io/acme/repo:tag",
      defaultValue: currentImage,
      confirmText: "Save",
    });
    if (!imageValue) {
      flash("Docker destination requires an image", "error");
      return false;
    }
    body.image = imageValue.trim();
    const configureAdvanced = await confirmModal(
      "Configure optional docker fields (container name, env passthrough, mounts)?",
      {
        confirmText: "Configure",
        cancelText: "Skip",
        danger: false,
      }
    );
    if (configureAdvanced) {
      const currentContainerName =
        typeof repo.effective_destination?.container_name === "string"
          ? String(repo.effective_destination.container_name)
          : "";
      const containerNameValue = await inputModal(
        "Docker container name (optional):",
        {
          placeholder: "car-runner",
          defaultValue: currentContainerName,
          confirmText: "Next",
          allowEmpty: true,
        }
      );
      if (containerNameValue === null) return false;
      const containerName = containerNameValue.trim();
      if (containerName) {
        body.container_name = containerName;
      }

      const envPassthroughValue = await inputModal(
        "Docker env passthrough (optional, comma-separated):",
        {
          placeholder: "CAR_*, PATH",
          defaultValue: currentDockerEnvPassthrough(repo.effective_destination),
          confirmText: "Next",
          allowEmpty: true,
        }
      );
      if (envPassthroughValue === null) return false;
      const envPassthrough = splitCommaSeparated(envPassthroughValue);
      if (envPassthrough.length) {
        body.env_passthrough = envPassthrough;
      }

      const mountsValue = await inputModal(
        "Docker mounts (optional, source:target pairs, comma-separated):",
        {
          placeholder: "/host/path:/workspace/path",
          defaultValue: currentDockerMounts(repo.effective_destination),
          confirmText: "Save",
          allowEmpty: true,
        }
      );
      if (mountsValue === null) return false;
      const parsedMounts = parseDockerMountList(mountsValue);
      if (parsedMounts.error) {
        flash(parsedMounts.error, "error");
        return false;
      }
      if (parsedMounts.mounts.length) {
        body.mounts = parsedMounts.mounts;
      }
    }
  }

  const payload = (await api(`/hub/repos/${encodeURIComponent(repo.id)}/destination`, {
    method: "POST",
    body,
  })) as HubDestinationResponse;
  const effective = formatDestinationSummary(payload.effective_destination);
  flash(`Updated destination for ${repo.id}: ${effective}`, "success");
  return true;
}

function renderHubChannelEntries(entries: HubChannelEntry[]): void {
  if (!hubChannelListEl) return;
  if (!entries.length) {
    hubChannelListEl.innerHTML =
      '<div class="muted small">No channel entries found.</div>';
    return;
  }

  const rows = entries
    .map((row) => {
      const label =
        typeof row.display === "string" && row.display.trim()
          ? row.display.trim()
          : row.key;
      const seen = row.seen_at ? formatTimeCompact(row.seen_at) : "unknown";
      return `
        <div class="hub-channel-row">
          <div class="hub-channel-main">
            <div class="hub-channel-key">${escapeHtml(row.key)}</div>
            <div class="hub-channel-meta muted small">${escapeHtml(
              label
            )} · seen ${escapeHtml(seen)}</div>
          </div>
          <button class="ghost sm" data-action="copy_channel_key" data-key="${escapeHtml(
            row.key
          )}">Copy</button>
        </div>
      `;
    })
    .join("");
  hubChannelListEl.innerHTML = rows;
}

async function loadHubChannelDirectory({ silent = false }: { silent?: boolean } = {}): Promise<void> {
  const query = (hubChannelQueryInput?.value || "").trim();
  try {
    const limit = resolveHubChannelLimit();
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    if (query) params.set("query", query);
    if (hubChannelRefreshBtn) hubChannelRefreshBtn.disabled = true;
    if (hubChannelSearchBtn) hubChannelSearchBtn.disabled = true;
    if (hubChannelLimitInput) hubChannelLimitInput.disabled = true;
    const payload = (await api(`/hub/chat/channels?${params.toString()}`, {
      method: "GET",
    })) as HubChannelDirectoryResponse;
    renderHubChannelEntries(Array.isArray(payload.entries) ? payload.entries : []);
  } catch (err) {
    if (!silent) {
      flash((err as Error).message || "Failed to load channel directory", "error");
    }
  } finally {
    if (hubChannelRefreshBtn) hubChannelRefreshBtn.disabled = false;
    if (hubChannelSearchBtn) hubChannelSearchBtn.disabled = false;
    if (hubChannelLimitInput) hubChannelLimitInput.disabled = false;
  }
}

async function copyTextToClipboard(value: string): Promise<void> {
  const text = String(value || "");
  if (!text) return;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

async function removeRepoWithChecks(repoId: string): Promise<void> {
  const check = await api(`/hub/repos/${repoId}/remove-check`, {
    method: "GET",
  });
  const warnings: string[] = [];
  const dirty = (check as { is_clean?: boolean }).is_clean === false;
  if (dirty) {
    warnings.push("Working tree has uncommitted changes.");
  }
  const upstream = (check as {
    upstream?: { has_upstream?: boolean; ahead?: number; behind?: number };
  }).upstream;
  const hasUpstream = upstream?.has_upstream === false;
  if (hasUpstream) {
    warnings.push("No upstream tracking branch is configured.");
  }
  const ahead = Number(upstream?.ahead || 0);
  if (ahead > 0) {
    warnings.push(`Local branch is ahead of upstream by ${ahead} commit(s).`);
  }
  const behind = Number(upstream?.behind || 0);
  if (behind > 0) {
    warnings.push(`Local branch is behind upstream by ${behind} commit(s).`);
  }
  const worktrees = Array.isArray((check as { worktrees?: string[] }).worktrees)
    ? (check as { worktrees?: string[] }).worktrees
    : [];
  if (worktrees.length) {
    warnings.push(`This repo has ${worktrees.length} worktree(s).`);
  }

  const messageParts = [`Remove repo "${repoId}" and delete its local directory?`];
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
}

async function handleRepoAction(repoId: string, action: string): Promise<void> {
  const buttons = repoListEl?.querySelectorAll(
    `button[data-repo="${repoId}"][data-action="${action}"]`
  );
  buttons?.forEach((btn) => (btn as HTMLButtonElement).disabled = true);
  try {
    if (action === "pin_parent" || action === "unpin_parent") {
      const pinned = action === "pin_parent";
      await setParentRepoPinned(repoId, pinned);
      renderReposWithScroll(hubData.repos || []);
      flash(`${pinned ? "Pinned" : "Unpinned"}: ${repoId}`, "success");
      return;
    }

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
    if (action === "repo_settings") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      if (!repo) {
        flash(`Repo not found: ${repoId}`, "error");
        return;
      }
      await openRepoSettingsModal(repo);
      return;
    }
    if (action === "set_destination") {
      const repo = hubData.repos.find((item) => item.id === repoId);
      if (!repo) {
        flash(`Repo not found: ${repoId}`, "error");
        return;
      }
      const updated = await promptAndSetRepoDestination(repo);
      if (updated) {
        await refreshHub();
      }
      return;
    }
    if (action === "cleanup_worktree") {
      const displayName = repoId.includes("--")
        ? repoId.split("--").pop()
        : repoId;
      const ok = await confirmModal(
        `Clean up worktree "${displayName}"?\n\nCAR will archive its runtime files for later viewing in the Archive tab, then remove the worktree directory and branch.`,
        { confirmText: "Archive & remove" }
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
      await removeRepoWithChecks(repoId);
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
  if (hubChannelSearchBtn) {
    hubChannelSearchBtn.addEventListener("click", () => {
      loadHubChannelDirectory().catch(() => {});
    });
  }
  if (hubChannelRefreshBtn) {
    hubChannelRefreshBtn.addEventListener("click", () => {
      loadHubChannelDirectory().catch(() => {});
    });
  }
  if (hubChannelQueryInput) {
    hubChannelQueryInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadHubChannelDirectory().catch(() => {});
      }
    });
  }
  if (hubChannelLimitInput) {
    hubChannelLimitInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadHubChannelDirectory().catch(() => {});
      }
    });
  }
  if (hubChannelListEl) {
    hubChannelListEl.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      const btn = target.closest(
        'button[data-action="copy_channel_key"]'
      ) as HTMLButtonElement | null;
      if (!btn) return;
      const key = String(btn.dataset.key || "").trim();
      if (!key) return;
      copyTextToClipboard(key)
        .then(() => flash(`Copied key: ${key}`, "success"))
        .catch((err) =>
          flash((err as Error).message || "Failed to copy key", "error")
        );
    });
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
    applyHubData(data);
    markHubRefreshed();
    saveSessionCache(HUB_CACHE_KEY, hubData);
    renderSummary(hubData.repos || []);
    renderReposWithScroll(hubData.repos || []);
    await loadHubUsage({ silent: true, allowRetry: false });
    await loadHubChannelDirectory({ silent: true });
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
  try {
    const data = await api("/hub/version", { method: "GET" });
    const version = (data as { asset_version?: string }).asset_version || "";
    const formatted = version ? `v${version}` : "v–";
    if (hubVersionEl) hubVersionEl.textContent = formatted;
    if (pmaVersionEl) pmaVersionEl.textContent = formatted;
  } catch (_err) {
    if (hubVersionEl) hubVersionEl.textContent = "v–";
    if (pmaVersionEl) pmaVersionEl.textContent = "v–";
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
  attachHubHandlers();
  initHubRepoListControls();
  if (!repoListEl) return;
  initHubUsageChartControls();
  initNotificationBell();
  const cachedHub = loadSessionCache<HubData | null>(HUB_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedHub) {
    applyHubData(cachedHub);
    renderSummary(hubData.repos || []);
    renderReposWithScroll(hubData.repos || []);
  }
  const cachedUsage = loadSessionCache<HubUsageData | null>(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
  if (cachedUsage) {
    indexHubUsage(cachedUsage);
    renderHubUsageMeta(cachedUsage);
  }
  loadHubUsageSeries();
  loadHubChannelDirectory({ silent: true }).catch(() => {});
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
};
