// GENERATED FILE - do not edit directly. Source: static_src/
import { loadPendingTurn, clearPendingTurn } from "./turnResume.js";
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
