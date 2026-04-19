import { api, flash, confirmModal } from "./utils.js";
import { HUB_CACHE_TTL_MS, HUB_USAGE_CACHE_KEY, saveSessionCache, loadSessionCache, loadHubBootstrapCache, saveHubBootstrapCache, indexHubUsage, } from "./hubCache.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { renderReposWithScroll, renderAgentWorkspaces, renderSummary, } from "./hubRepoCards.js";
import { describeUpdateTarget, getUpdateTarget, includesWebUpdateTarget, normalizeUpdateTarget, updateRestartNotice, updateTargetOptionsFromResponse, } from "./updateTargets.js";
import { getHubData, applyHubData, getHubChannelEntries, getPinnedParentRepoIds, startHubJob, } from "./hubActions.js";
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
export async function loadUpdateTargetOptions(selectId) {
    const select = selectId ? document.getElementById(selectId) : null;
    if (!select)
        return;
    const isInitialized = select.dataset.updateTargetsInitialized === "1";
    let payload;
    try {
        payload = await api("/system/update/targets", { method: "GET" });
    }
    catch (_err) {
        return;
    }
    const { options, defaultTarget } = updateTargetOptionsFromResponse(payload);
    if (!options.length)
        return;
    const previous = normalizeUpdateTarget(select.value || "all");
    const hasPrevious = options.some((item) => item.value === previous);
    const fallback = options.some((item) => item.value === defaultTarget)
        ? defaultTarget
        : options[0].value;
    select.replaceChildren();
    options.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.label;
        select.appendChild(option);
    });
    if (isInitialized) {
        select.value = hasPrevious ? previous : fallback;
    }
    else {
        select.value = fallback;
        select.dataset.updateTargetsInitialized = "1";
    }
}
export async function handleSystemUpdate(btnId, targetSelectId) {
    const btn = document.getElementById(btnId);
    if (!btn)
        return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Checking...";
    const updateTarget = getUpdateTarget(targetSelectId);
    const targetLabel = describeUpdateTarget(updateTarget);
    let check;
    try {
        check = await api("/system/update/check");
    }
    catch (err) {
        check = { update_available: true, message: err.message || "Unable to check for updates." };
    }
    if (!check?.update_available) {
        flash(check?.message || "No update available.", "info");
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    const restartNotice = updateRestartNotice(updateTarget);
    const confirmed = await confirmModal(`${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`);
    if (!confirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    btn.textContent = "Updating...";
    try {
        let res = await api("/system/update", {
            method: "POST",
            body: { target: updateTarget },
        });
        if (res.requires_confirmation) {
            const forceConfirmed = await confirmModal(res.message || "Active sessions are still running. Update anyway?", { confirmText: "Update anyway", cancelText: "Cancel", danger: true });
            if (!forceConfirmed) {
                btn.disabled = false;
                btn.textContent = originalText;
                return;
            }
            res = await api("/system/update", {
                method: "POST",
                body: { target: updateTarget, force: true },
            });
        }
        flash(res.message || `Update started (${targetLabel}).`, "success");
        if (!includesWebUpdateTarget(updateTarget)) {
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        document.body.style.pointerEvents = "none";
        setTimeout(() => {
            const url = new URL(window.location.href);
            url.searchParams.set("v", String(Date.now()));
            window.location.replace(url.toString());
        }, 8000);
    }
    catch (err) {
        flash(err.message || "Update failed", "error");
        btn.disabled = false;
        btn.textContent = originalText;
    }
}
export function bootstrapHubData() {
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
