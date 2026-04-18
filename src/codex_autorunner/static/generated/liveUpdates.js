// GENERATED FILE - do not edit directly. Source: static_src/
import { publish, subscribe } from "./bus.js";
const INVALIDATION_DEBOUNCE_MS = 750;
let initialized = false;
let lastState = null;
let flushTimer = null;
const pendingInvalidations = new Set();
function normalizeState(payload) {
    return {
        last_run_id: payload.last_run_id ?? null,
        last_run_finished_at: payload.last_run_finished_at ?? null,
        outstanding_count: payload.outstanding_count ?? null,
        done_count: payload.done_count ?? null,
        status: payload.status ?? null,
        runner_pid: payload.runner_pid ?? null,
    };
}
function queueInvalidation(key) {
    pendingInvalidations.add(key);
    if (flushTimer)
        return;
    flushTimer = setTimeout(flushInvalidations, INVALIDATION_DEBOUNCE_MS);
}
function flushInvalidations() {
    flushTimer = null;
    if (!pendingInvalidations.size)
        return;
    const keys = Array.from(pendingInvalidations);
    pendingInvalidations.clear();
    keys.forEach((key) => publish(key, { source: "state" }));
}
function handleStateUpdate(payload) {
    if (!payload || typeof payload !== "object")
        return;
    const next = normalizeState(payload);
    if (!lastState) {
        lastState = next;
        return;
    }
    if (lastState.last_run_id !== next.last_run_id ||
        lastState.last_run_finished_at !== next.last_run_finished_at) {
        queueInvalidation("runs:invalidate");
    }
    if (lastState.outstanding_count !== next.outstanding_count ||
        lastState.done_count !== next.done_count) {
        queueInvalidation("todo:invalidate");
    }
    if (lastState.status !== next.status ||
        lastState.runner_pid !== next.runner_pid) {
        queueInvalidation("runner:status");
    }
    lastState = next;
}
export function initLiveUpdates() {
    if (initialized)
        return;
    initialized = true;
    subscribe("state:update", handleStateUpdate);
}
