import { loadPendingTurn, clearPendingTurn, createTurnRecoveryTracker, DEFAULT_RECOVERY_MAX_ATTEMPTS, } from "./turnResume.js";
import { streamTurnEvents } from "./fileChat.js";
export function createTurnEventsController() {
    let controller = null;
    return {
        get current() {
            return controller;
        },
        set current(value) {
            controller = value;
        },
        abort() {
            if (controller) {
                try {
                    controller.abort();
                }
                catch {
                    // ignore
                }
                controller = null;
            }
        },
    };
}
export function startTurnEventsStream(ctrl, meta, options = {}) {
    const threadId = meta.threadId;
    const turnId = meta.turnId;
    if (!threadId || !turnId)
        return;
    ctrl.abort();
    ctrl.current = streamTurnEvents({
        agent: meta.agent,
        threadId,
        turnId,
    }, {
        onEvent: options.onEvent,
    });
}
export function clearManagedTurn(ctrl, pendingKey) {
    ctrl.abort();
    clearPendingTurn(pendingKey);
}
export function loadManagedPendingTurn(key) {
    return loadPendingTurn(key);
}
export { loadPendingTurn, savePendingTurn, clearPendingTurn } from "./turnResume.js";
export { createTurnRecoveryTracker, DEFAULT_RECOVERY_MAX_ATTEMPTS, } from "./turnResume.js";
/**
 * Unified Active-Turn Surface Policy
 *
 * | Situation            | Behavior                                          |
 * |----------------------|---------------------------------------------------|
 * | Turn running         | Abort controller + fire-and-forget server         |
 * | + user sends         | interrupt + clear pending, then immediately send  |
 * |                      | the new message.                                  |
 * | Turn running         | Abort controller + interrupt server + clear        |
 * | + user cancels       | pending. Show "Cancelled" status.                  |
 * | Recovery pending     | Retry up to max attempts. On stale: show error     |
 * | + max exceeded       | with "retry or new thread" guidance.               |
 * | Recovery pending     | Clear pending, return to idle.                     |
 * | + user discards      |                                                   |
 */
export const ACTIVE_TURN_RECOVERY_STALE_MESSAGE = "Could not recover previous turn. Send a new message to retry or start a new thread.";
export function cancelActiveTurnSync(options) {
    options.abortController();
    options.turnEventsCtrl.abort();
    options.clearPending?.();
    if (options.interruptServer) {
        void options.interruptServer().catch(() => { });
    }
}
export function scheduleRecoveryRetry(opts) {
    const { tracker, retryFn, onStale, intervalMs = 1000 } = opts;
    if (tracker.phase !== "recovering")
        return;
    if (!tracker.tick()) {
        onStale?.();
        return;
    }
    window.setTimeout(() => void retryFn(), intervalMs);
}
export const __turnRecoveryPolicyTest = {
    createTurnRecoveryTracker,
    cancelActiveTurnSync,
    scheduleRecoveryRetry,
    ACTIVE_TURN_RECOVERY_STALE_MESSAGE,
    DEFAULT_RECOVERY_MAX_ATTEMPTS,
};
