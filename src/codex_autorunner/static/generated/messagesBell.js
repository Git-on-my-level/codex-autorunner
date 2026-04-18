// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
let bellInitialized = false;
let activeRunId = null;
let messageBellCleanup = null;
function setBadge(count) {
    const badge = document.getElementById("tab-badge-inbox");
    if (!badge)
        return;
    if (count > 0) {
        badge.textContent = String(count);
        badge.classList.remove("hidden");
    }
    else {
        badge.textContent = "";
        badge.classList.add("hidden");
    }
}
function clearActiveRun() {
    activeRunId = null;
    setBadge(0);
}
export function getActiveMessageRunId() {
    return activeRunId;
}
export async function refreshBell() {
    if (!isRepoHealthy()) {
        clearActiveRun();
        return;
    }
    try {
        const res = (await api("/api/messages/active"));
        if (res?.active && res.run_id) {
            activeRunId = res.run_id;
            setBadge(1);
        }
        else {
            clearActiveRun();
        }
    }
    catch (_err) {
        clearActiveRun();
    }
}
export function initMessageBell() {
    if (bellInitialized)
        return;
    bellInitialized = true;
    if (messageBellCleanup) {
        messageBellCleanup();
    }
    messageBellCleanup = registerAutoRefresh("messages:bell", {
        callback: async (_ctx) => {
            if (!isRepoHealthy()) {
                clearActiveRun();
                return;
            }
            await refreshBell();
        },
        interval: CONSTANTS.UI.POLLING_INTERVAL,
        refreshOnActivation: true,
        immediate: true,
    });
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
        if (status === "ok" || status === "degraded") {
            void refreshBell();
        }
    });
}
