import { publish, subscribe } from "./bus.js";

type InvalidationKey = "runs:invalidate" | "todo:invalidate" | "runner:status";

interface StateSnapshot {
  last_run_id?: number | null;
  last_run_finished_at?: string | null;
  outstanding_count?: number | null;
  done_count?: number | null;
  status?: string | null;
  runner_pid?: number | null;
}

const INVALIDATION_DEBOUNCE_MS = 750;

let initialized = false;
let lastState: StateSnapshot | null = null;
let flushTimer: ReturnType<typeof setTimeout> | null = null;
const pendingInvalidations = new Set<InvalidationKey>();

function normalizeState(payload: StateSnapshot): StateSnapshot {
  return {
    last_run_id: payload.last_run_id ?? null,
    last_run_finished_at: payload.last_run_finished_at ?? null,
    outstanding_count: payload.outstanding_count ?? null,
    done_count: payload.done_count ?? null,
    status: payload.status ?? null,
    runner_pid: payload.runner_pid ?? null,
  };
}

function queueInvalidation(key: InvalidationKey): void {
  pendingInvalidations.add(key);
  if (flushTimer) return;
  flushTimer = setTimeout(flushInvalidations, INVALIDATION_DEBOUNCE_MS);
}

function flushInvalidations(): void {
  flushTimer = null;
  if (!pendingInvalidations.size) return;
  const keys = Array.from(pendingInvalidations);
  pendingInvalidations.clear();
  keys.forEach((key) => publish(key, { source: "state" }));
}

function handleStateUpdate(payload: unknown): void {
  if (!payload || typeof payload !== "object") return;
  const next = normalizeState(payload as StateSnapshot);
  if (!lastState) {
    lastState = next;
    return;
  }
  if (
    lastState.last_run_id !== next.last_run_id ||
    lastState.last_run_finished_at !== next.last_run_finished_at
  ) {
    queueInvalidation("runs:invalidate");
  }
  if (
    lastState.outstanding_count !== next.outstanding_count ||
    lastState.done_count !== next.done_count
  ) {
    queueInvalidation("todo:invalidate");
  }
  if (
    lastState.status !== next.status ||
    lastState.runner_pid !== next.runner_pid
  ) {
    queueInvalidation("runner:status");
  }
  lastState = next;
}

export function initLiveUpdates(): void {
  if (initialized) return;
  initialized = true;
  subscribe("state:update", handleStateUpdate);
}
