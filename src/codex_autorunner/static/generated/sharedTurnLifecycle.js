// GENERATED FILE - do not edit directly. Source: static_src/
import * as turnResumeModule from "./turnResume.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { streamTurnEvents } from "./fileChat.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
// Browsers can briefly combine a fresh module graph with one stale generated chunk
// after a deploy or local rebuild. Namespace-import the recovery helpers so a stale
// `turnResume.js` cannot crash PMA during ESM linking with a missing named export.
const DEFAULT_RECOVERY_MAX_ATTEMPTS = typeof turnResumeModule.DEFAULT_RECOVERY_MAX_ATTEMPTS === "number"
    ? turnResumeModule.DEFAULT_RECOVERY_MAX_ATTEMPTS
    : 30;
export { DEFAULT_RECOVERY_MAX_ATTEMPTS };
function createFallbackTurnRecoveryTracker(maxAttempts) {
    let phase = "recovering";
    let attempts = 0;
    const max = maxAttempts ?? DEFAULT_RECOVERY_MAX_ATTEMPTS;
    return {
        get phase() {
            return phase;
        },
        get attempts() {
            return attempts;
        },
        get maxAttempts() {
            return max;
        },
        tick() {
            if (phase !== "recovering")
                return false;
            attempts += 1;
            if (attempts >= max) {
                phase = "stale";
                return false;
            }
            return true;
        },
    };
}
export function loadPendingTurn(key) {
    if (typeof turnResumeModule.loadPendingTurn === "function") {
        return turnResumeModule.loadPendingTurn(key);
    }
    return null;
}
export function savePendingTurn(key, turn) {
    if (typeof turnResumeModule.savePendingTurn === "function") {
        turnResumeModule.savePendingTurn(key, turn);
    }
}
export function clearPendingTurn(key) {
    if (typeof turnResumeModule.clearPendingTurn === "function") {
        turnResumeModule.clearPendingTurn(key);
    }
}
export function createTurnRecoveryTracker(maxAttempts) {
    if (typeof turnResumeModule.createTurnRecoveryTracker === "function") {
        return turnResumeModule.createTurnRecoveryTracker(maxAttempts);
    }
    return createFallbackTurnRecoveryTracker(maxAttempts);
}
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
export async function cancelActiveTurnAndWait(options) {
    options.abortController();
    options.turnEventsCtrl.abort();
    options.clearPending?.();
    if (!options.interruptServer) {
        return;
    }
    try {
        await options.interruptServer();
    }
    catch {
        // ignore
    }
}
export function pendingTurnMatches(expected, actual) {
    if (!expected || !actual) {
        return false;
    }
    return (expected.clientTurnId === actual.clientTurnId &&
        expected.startedAtMs === actual.startedAtMs &&
        (expected.target || "") === (actual.target || ""));
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
    cancelActiveTurnAndWait,
    pendingTurnMatches,
    scheduleRecoveryRetry,
    ACTIVE_TURN_RECOVERY_STALE_MESSAGE,
    DEFAULT_RECOVERY_MAX_ATTEMPTS,
};
