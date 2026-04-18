export type FlowEvent = {
  seq?: number;
  event_type: string;
  timestamp: string;
  data?: Record<string, unknown>;
  step_id?: string;
};

export type WorkerHealth = {
  status?: string;
  pid?: number | null;
  is_alive?: boolean;
  message?: string | null;
};

export type FlowRun = {
  id?: string;
  status?: string;
  state?: Record<string, unknown>;
  error_message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  last_event_seq?: number | null;
  last_event_at?: string | null;
  reason_summary?: string | null;
  worker_health?: WorkerHealth | null;
};

export type BootstrapResponse = FlowRun & {
  state?: Record<string, unknown> & { hint?: string };
};

export type BootstrapCheckResponse = {
  status: "ready" | "needs_issue";
  github_available?: boolean;
  repo?: string | null;
};

export type TicketFile = {
  path?: string;
  index?: number | null;
  frontmatter?: Record<string, unknown> | null;
  body?: string | null;
  errors?: string[];
  diff_stats?: {
    insertions: number;
    deletions: number;
    files_changed: number;
  } | null;
  duration_seconds?: number | null;
};

export type DispatchAttachment = {
  name?: string;
  rel_path?: string;
  path?: string;
  size?: number | null;
  url?: string;
};

export type DispatchEntry = {
  seq?: string;
  dispatch?: {
    mode?: string;
    title?: string;
    body?: string;
    extra?: Record<string, unknown>;
    diff_stats?: {
      insertions: number;
      deletions: number;
      files_changed: number;
    } | null;
    is_handoff?: boolean;
  } | null;
  errors?: string[];
  attachments?: DispatchAttachment[];
  created_at?: string | null;
};

export type TicketListPayload = {
  tickets?: TicketFile[];
  lint_errors?: string[];
  activeTicket?: string | null;
  flowStatus?: string | null;
};

export type DispatchHistoryPayload = {
  runId: string | null;
  history?: DispatchEntry[];
};

export const DISPATCH_PANEL_COLLAPSED_KEY = "car-dispatch-panel-collapsed";
export const LAST_SEEN_SEQ_KEY_PREFIX = "car-ticket-flow-last-seq:";
export const EVENT_STREAM_RETRY_DELAYS_MS = [500, 1000, 2000, 5000, 10000];
export const STALE_THRESHOLD_MS = 30000;
export const MAX_OUTPUT_LINES = 200;
export const LIVE_EVENT_MAX = 50;
export const MAX_REASON_LENGTH = 60;

const lastSeenSeqByRun: Record<string, number> = {};

export function isFlowActiveStatus(status: string | null): boolean {
  return status === "pending" || status === "running" || status === "stopping";
}

export function getLastSeenSeq(runId: string): number | null {
  if (lastSeenSeqByRun[runId] !== undefined) {
    return lastSeenSeqByRun[runId] as number;
  }
  const stored = localStorage.getItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`);
  if (!stored) return null;
  const parsed = Number.parseInt(stored, 10);
  if (Number.isNaN(parsed)) return null;
  lastSeenSeqByRun[runId] = parsed;
  return parsed;
}

export function setLastSeenSeq(runId: string, seq: number): void {
  if (!Number.isFinite(seq)) return;
  const current = lastSeenSeqByRun[runId];
  if (current !== undefined && seq <= current) return;
  lastSeenSeqByRun[runId] = seq;
  localStorage.setItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`, String(seq));
}

export function parseEventSeq(event: FlowEvent, lastEventId?: string | null): number | null {
  if (typeof event.seq === "number" && Number.isFinite(event.seq)) {
    return event.seq;
  }
  if (lastEventId) {
    const parsed = Number.parseInt(lastEventId, 10);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return null;
}

export function formatElapsedSeconds(totalSeconds: number): string {
  const diffSecs = Math.max(0, Math.floor(totalSeconds));

  if (diffSecs < 60) {
    return `${diffSecs}s`;
  }
  const mins = Math.floor(diffSecs / 60);
  const secs = diffSecs % 60;
  if (mins < 60) {
    return secs === 0 ? `${mins}m` : `${mins}m ${secs}s`;
  }
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;
  if (hours < 24) {
    return remainingMins === 0 ? `${hours}h` : `${hours}h ${remainingMins}m`;
  }
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return remainingHours === 0 ? `${days}d` : `${days}d ${remainingHours}h`;
}

export function formatElapsed(startTime: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - startTime.getTime();
  return formatElapsedSeconds(diffMs / 1000);
}

export function formatDispatchTime(ts?: string | null): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  if (diffSecs < 60) return "now";
  const diffMins = Math.floor(diffSecs / 60);
  if (diffMins < 60) return `${diffMins}m`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function formatNumber(n: number): string {
  if (n >= 1000000) {
    return `${(n / 1000000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (n >= 1000) {
    return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  }
  return n.toString();
}

export function diffStatsSignature(
  diffStats?:
    | { insertions?: number; deletions?: number; files_changed?: number }
    | null
): string {
  if (!diffStats) return "";
  return [
    diffStats.insertions || 0,
    diffStats.deletions || 0,
    diffStats.files_changed || 0,
  ].join(",");
}

export function formatTimeAgo(timestamp: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - timestamp.getTime();
  const diffSecs = Math.floor(diffMs / 1000);

  if (diffSecs < 5) return "just now";
  if (diffSecs < 60) return `${diffSecs}s ago`;
  const mins = Math.floor(diffSecs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ago`;
}

export function truncate(text: string, max = 100): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}…`;
}
