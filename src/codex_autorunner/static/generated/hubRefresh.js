// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash } from "./utils.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { HUB_CACHE_TTL_MS, HUB_USAGE_CACHE_KEY, saveSessionCache, loadSessionCache, loadHubBootstrapCache, saveHubBootstrapCache, indexHubUsage, } from "./hubCache.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { registerAutoRefresh, setAutoRefreshEnabled } from "./autoRefresh.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { isUiMockActive } from "./uiMock.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { renderReposWithScroll, renderAgentWorkspaces, renderSummary, } from "./hubRepoCards.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { loadUpdateTargetOptions, handleSystemUpdate, } from "./systemUpdateUi.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { getHubData, applyHubData, getHubChannelEntries, getPinnedParentRepoIds, startHubJob, } from "./hubActions.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
export const HUB_REFRESH_ACTIVE_MS = 5000;
export const HUB_REFRESH_IDLE_MS = 30000;
const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
const hubVersionEl = document.getElementById("hub-version");
const pmaVersionEl = document.getElementById("pma-version");
const hubUsageSummaryRetryTimer = { current: null };
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
let lastHubAutoRefreshAt = 0;
function setButtonLoading(scanning) {
    const buttons = [document.getElementById("hub-refresh")];
    buttons.forEach((btn) => {
        if (!btn)
            return;
        btn.disabled = scanning;
        if (scanning) {
            btn.classList.add("loading");
        }
        else {
            btn.classList.remove("loading");
        }
    });
}
function renderHubUsageMeta(data) {
    if (hubUsageMeta) {
        hubUsageMeta.textContent = data?.codex_home || "–";
    }
}
function scheduleHubUsageSummaryRetry() {
    clearHubUsageSummaryRetry();
    hubUsageSummaryRetryTimer.current = setTimeout(() => {
        loadHubUsage();
    }, 1500);
}
function clearHubUsageSummaryRetry() {
    if (hubUsageSummaryRetryTimer.current) {
        clearTimeout(hubUsageSummaryRetryTimer.current);
        hubUsageSummaryRetryTimer.current = null;
    }
}
function handleHubUsagePayload(data, { cachedUsage, allowRetry }) {
    const hasSummary = data && Array.isArray(data.repos);
    const effective = hasSummary ? data : cachedUsage;
    const hubData = getHubData();
    const hubChannelEntries = getHubChannelEntries();
    const pinnedParentRepoIds = getPinnedParentRepoIds();
    if (effective) {
        indexHubUsage(effective);
        renderHubUsageMeta(effective);
        renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    }
    if (data?.status === "loading") {
        if (allowRetry)
            scheduleHubUsageSummaryRetry();
        return Boolean(hasSummary);
    }
    if (hasSummary) {
        clearHubUsageSummaryRetry();
        return true;
    }
    if (!effective && !data) {
        renderReposWithScroll(hubData.repos || [], hubChannelEntries, pinnedParentRepoIds);
    }
    return false;
}
export async function loadHubUsage({ silent = false, allowRetry = true } = {}) {
    if (!silent && hubUsageRefresh)
        hubUsageRefresh.disabled = true;
    try {
        const data = await api("/hub/usage");
        const cachedUsage = loadSessionCache(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
        const shouldCache = handleHubUsagePayload(data, {
            cachedUsage,
            allowRetry,
        });
        if (shouldCache) {
            saveSessionCache(HUB_USAGE_CACHE_KEY, data);
        }
    }
    catch (err) {
        const cachedUsage = loadSessionCache(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
        if (cachedUsage) {
            handleHubUsagePayload(cachedUsage, { cachedUsage, allowRetry: false });
        }
        if (!silent) {
            flash(err.message || "Failed to load usage", "error");
        }
        clearHubUsageSummaryRetry();
    }
    finally {
        if (!silent && hubUsageRefresh)
            hubUsageRefresh.disabled = false;
    }
}
async function loadHubChannelDirectory({ silent = false } = {}) {
    try {
        const payload = (await api("/hub/chat/channels?limit=1000", {
            method: "GET",
        }));
        const hubData = getHubData();
        const pinnedParentRepoIds = getPinnedParentRepoIds();
        renderReposWithScroll(hubData.repos || [], payload.entries || [], pinnedParentRepoIds);
    }
    catch (err) {
        if (!silent) {
            flash(err.message || "Failed to load channel directory", "error");
        }
    }
}
export async function refreshHub() {
    setButtonLoading(true);
    try {
        const data = await api("/hub/repos", { method: "GET" });
        applyHubData(data);
        markHubRefreshed();
        const hubData = getHubData();
        saveHubBootstrapCache(hubData);
        renderSummary(hubData.repos || [], hubData);
        renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        renderAgentWorkspaces(hubData.agent_workspaces || [], getHubChannelEntries());
        loadHubUsage({ silent: true }).catch(() => { });
        loadHubChannelDirectory({ silent: true }).catch(() => { });
    }
    catch (err) {
        flash(err.message || "Hub request failed", "error");
    }
    finally {
        setButtonLoading(false);
    }
}
export async function triggerHubScan() {
    setButtonLoading(true);
    try {
        await startHubJob("/hub/jobs/scan", { startedMessage: "Hub scan queued" });
        await refreshHub();
    }
    catch (err) {
        flash(err.message || "Hub scan failed", "error");
    }
    finally {
        setButtonLoading(false);
    }
}
function markHubRefreshed() {
    lastHubAutoRefreshAt = Date.now();
}
function hasActiveRuns(repos) {
    return repos.some((repo) => repo.status === "running");
}
async function dynamicRefreshHub() {
    const now = Date.now();
    const hubData = getHubData();
    const running = hasActiveRuns(hubData.repos || []);
    const minInterval = running ? HUB_REFRESH_ACTIVE_MS : HUB_REFRESH_IDLE_MS;
    if (now - lastHubAutoRefreshAt < minInterval)
        return;
    await silentRefreshHub();
}
async function silentRefreshHub() {
    try {
        const data = await api("/hub/repos", { method: "GET" });
        applyHubData(data);
        markHubRefreshed();
        const hubData = getHubData();
        saveHubBootstrapCache(hubData);
        renderSummary(hubData.repos || [], hubData);
        renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        renderAgentWorkspaces(hubData.agent_workspaces || [], getHubChannelEntries());
        await Promise.allSettled([
            loadHubUsage({ silent: true, allowRetry: false }),
            loadHubChannelDirectory({ silent: true }),
        ]);
    }
    catch (err) {
        console.error("Auto-refresh hub failed:", err);
    }
}
async function loadHubVersion() {
    try {
        const data = await api("/hub/version", { method: "GET" });
        const version = data.asset_version || "";
        const formatted = version ? `v${version}` : "v–";
        if (hubVersionEl)
            hubVersionEl.textContent = formatted;
        if (pmaVersionEl)
            pmaVersionEl.textContent = formatted;
    }
    catch (_err) {
        if (hubVersionEl)
            hubVersionEl.textContent = "v–";
        if (pmaVersionEl)
            pmaVersionEl.textContent = "v–";
    }
}
async function checkUpdateStatus() {
    try {
        const data = await api("/system/update/status", { method: "GET" });
        if (!data || !data.status)
            return;
        const stamp = data.at ? String(data.at) : "";
        if (stamp && sessionStorage.getItem(UPDATE_STATUS_SEEN_KEY) === stamp)
            return;
        if (data.status === "rollback" || data.status === "error") {
            flash(data.message || "Update failed; rollback attempted.", "error");
        }
        if (stamp)
            sessionStorage.setItem(UPDATE_STATUS_SEEN_KEY, stamp);
    }
    catch (_err) {
        // Ignore update status failures; UI still renders.
    }
}
export { loadUpdateTargetOptions, handleSystemUpdate };
export function bootstrapHubData() {
    if (isUiMockActive()) {
        setAutoRefreshEnabled(false, "ui mock");
    }
    const hubData = getHubData();
    const cachedHub = loadHubBootstrapCache();
    if (cachedHub) {
        applyHubData(cachedHub);
        renderSummary(hubData.repos || [], hubData);
        renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        renderAgentWorkspaces(hubData.agent_workspaces || [], getHubChannelEntries());
    }
    const cachedUsage = loadSessionCache(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (cachedUsage) {
        indexHubUsage(cachedUsage);
        renderHubUsageMeta(cachedUsage);
    }
    loadHubChannelDirectory({ silent: true }).catch(() => { });
    refreshHub();
    void Promise.allSettled([loadHubVersion(), checkUpdateStatus()]);
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
