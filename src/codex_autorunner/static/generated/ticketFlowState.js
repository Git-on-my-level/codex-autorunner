// GENERATED FILE - do not edit directly. Source: static_src/
export const DISPATCH_PANEL_COLLAPSED_KEY = "car-dispatch-panel-collapsed";
export const LAST_SEEN_SEQ_KEY_PREFIX = "car-ticket-flow-last-seq:";
export const EVENT_STREAM_RETRY_DELAYS_MS = [500, 1000, 2000, 5000, 10000];
export const STALE_THRESHOLD_MS = 30000;
export const MAX_OUTPUT_LINES = 200;
export const LIVE_EVENT_MAX = 50;
export const MAX_REASON_LENGTH = 60;
const lastSeenSeqByRun = {};
export function isFlowActiveStatus(status) {
    return status === "pending" || status === "running" || status === "stopping";
}
export function getLastSeenSeq(runId) {
    if (lastSeenSeqByRun[runId] !== undefined) {
        return lastSeenSeqByRun[runId];
    }
    const stored = localStorage.getItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`);
    if (!stored)
        return null;
    const parsed = Number.parseInt(stored, 10);
    if (Number.isNaN(parsed))
        return null;
    lastSeenSeqByRun[runId] = parsed;
    return parsed;
}
export function setLastSeenSeq(runId, seq) {
    if (!Number.isFinite(seq))
        return;
    const current = lastSeenSeqByRun[runId];
    if (current !== undefined && seq <= current)
        return;
    lastSeenSeqByRun[runId] = seq;
    localStorage.setItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`, String(seq));
}
export function parseEventSeq(event, lastEventId) {
    if (typeof event.seq === "number" && Number.isFinite(event.seq)) {
        return event.seq;
    }
    if (lastEventId) {
        const parsed = Number.parseInt(lastEventId, 10);
        if (!Number.isNaN(parsed))
            return parsed;
    }
    return null;
}
export function formatElapsedSeconds(totalSeconds) {
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
export function formatElapsed(startTime) {
    const now = new Date();
    const diffMs = now.getTime() - startTime.getTime();
    return formatElapsedSeconds(diffMs / 1000);
}
export function formatDispatchTime(ts) {
    if (!ts)
        return "";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime()))
        return "";
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSecs = Math.floor(diffMs / 1000);
    if (diffSecs < 60)
        return "now";
    const diffMins = Math.floor(diffSecs / 60);
    if (diffMins < 60)
        return `${diffMins}m`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24)
        return `${diffHours}h`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7)
        return `${diffDays}d`;
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
}
export function formatNumber(n) {
    if (n >= 1000000) {
        return `${(n / 1000000).toFixed(1).replace(/\.0$/, "")}M`;
    }
    if (n >= 1000) {
        return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
    }
    return n.toString();
}
export function diffStatsSignature(diffStats) {
    if (!diffStats)
        return "";
    return [
        diffStats.insertions || 0,
        diffStats.deletions || 0,
        diffStats.files_changed || 0,
    ].join(",");
}
export function formatTimeAgo(timestamp) {
    const now = new Date();
    const diffMs = now.getTime() - timestamp.getTime();
    const diffSecs = Math.floor(diffMs / 1000);
    if (diffSecs < 5)
        return "just now";
    if (diffSecs < 60)
        return `${diffSecs}s ago`;
    const mins = Math.floor(diffSecs / 60);
    if (mins < 60)
        return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ago`;
}
export function truncate(text, max = 100) {
    if (text.length <= max)
        return text;
    return `${text.slice(0, max).trim()}…`;
}
