// GENERATED FILE - do not edit directly. Source: static_src/
import { api } from "./utils.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { registerAutoRefresh } from "./autoRefresh.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { CONSTANTS } from "./constants.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { subscribe } from "./bus.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { isRepoHealthy } from "./health.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
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
