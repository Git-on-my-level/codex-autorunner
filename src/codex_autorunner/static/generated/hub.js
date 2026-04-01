// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, statusPill, resolvePath, escapeHtml, confirmModal, inputModal, openModal, } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { HUB_BASE } from "./env.js";
import { preserveScroll } from "./preserve.js";
import { initNotificationBell } from "./notificationBell.js";
import { describeUpdateTarget, getUpdateTarget, includesWebUpdateTarget, normalizeUpdateTarget, updateRestartNotice, updateTargetOptionsFromResponse, } from "./updateTargets.js";
function nonPmaChatBoundThreadCount(repo) {
    if (repo.non_pma_chat_bound_thread_count != null) {
        return Math.max(0, Number(repo.non_pma_chat_bound_thread_count || 0));
    }
    const totalCount = Number(repo.chat_bound_thread_count || 0);
    const pmaCount = Number(repo.pma_chat_bound_thread_count || 0);
    return Math.max(0, totalCount - pmaCount);
}
function isCleanupBlockedByChatBinding(repo) {
    if ((repo.kind || "base") !== "worktree")
        return false;
    if (repo.cleanup_blocked_by_chat_binding === true)
        return true;
    return nonPmaChatBoundThreadCount(repo) > 0;
}
function isChatBoundWorktree(repo) {
    return isCleanupBlockedByChatBinding(repo);
}
function unboundManagedThreadCount(repo) {
    return Math.max(0, Number(repo.unbound_managed_thread_count || 0));
}
function baseReposWithUnboundThreads(repos) {
    return repos.filter((repo) => (repo.kind || "base") === "base" && unboundManagedThreadCount(repo) > 0);
}
function totalUnboundManagedThreadCount(repos) {
    return repos.reduce((total, repo) => total + unboundManagedThreadCount(repo), 0);
}
function dirtyBaseReposWithUnboundThreads(repos) {
    return baseReposWithUnboundThreads(repos).filter((repo) => repo.is_clean === false);
}
const HUB_VIEW_PREFS_KEY = `car:hub-view-prefs:${HUB_BASE || "/"}`;
const HUB_DEFAULT_VIEW_PREFS = {
    flowFilter: "all",
    sortOrder: "repo_id",
};
let hubData = {
    repos: [],
    agent_workspaces: [],
    last_scan_at: null,
    pinned_parent_repo_ids: [],
};
const prefetchedUrls = new Set();
const hubViewPrefs = { ...HUB_DEFAULT_VIEW_PREFS };
let pinnedParentRepoIds = new Set();
const HUB_CACHE_TTL_MS = 30000;
const HUB_CACHE_KEY = `car:hub:${HUB_BASE || "/"}`;
const HUB_USAGE_CACHE_KEY = `car:hub-usage:${HUB_BASE || "/"}`;
const HUB_REFRESH_ACTIVE_MS = 5000;
const HUB_REFRESH_IDLE_MS = 30000;
let lastHubAutoRefreshAt = 0;
const repoListEl = document.getElementById("hub-repo-list");
const agentWorkspaceListEl = document.getElementById("hub-agent-workspace-list");
const lastScanEl = document.getElementById("hub-last-scan");
const pmaLastScanEl = document.getElementById("pma-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");
const hubUsageMeta = document.getElementById("hub-usage-meta");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
const hubVersionEl = document.getElementById("hub-version");
const pmaVersionEl = document.getElementById("pma-version");
const hubRepoSearchInput = document.getElementById("hub-repo-search");
const hubFlowFilterEl = document.getElementById("hub-flow-filter");
const hubSortOrderEl = document.getElementById("hub-sort-order");
const hubRepoPanelEl = document.getElementById("hub-repo-panel");
function getHubCleanupAllThreadsBtn() {
    return document.getElementById("hub-cleanup-all-threads");
}
const hubAgentPanelEl = document.getElementById("hub-agent-panel");
const hubShellEl = document.getElementById("hub-shell");
const hubRepoPanelSummaryEl = document.getElementById("hub-repo-panel-summary");
const hubAgentPanelSummaryEl = document.getElementById("hub-agent-panel-summary");
const hubRepoPanelStateEl = document.getElementById("hub-repo-panel-state");
const hubAgentPanelStateEl = document.getElementById("hub-agent-panel-state");
const UPDATE_STATUS_SEEN_KEY = "car_update_status_seen";
const HUB_PANEL_PREFS_KEY = `car:hub-open-panel:${HUB_BASE || "/"}`;
const HUB_JOB_POLL_INTERVAL_MS = 1200;
const HUB_JOB_TIMEOUT_MS = 180000;
let hubUsageSummaryRetryTimer = null;
let hubUsageIndex = {};
let hubUsageUnmatched = null;
let hubChannelEntries = [];
let hubOpenPanel = loadHubOpenPanel();
/** True while bulk cleanup POST is in flight; keeps the button disabled across hub re-renders. */
let cleanupAllRepoThreadsInFlight = false;
function saveSessionCache(key, value) {
    try {
        const payload = { at: Date.now(), value };
        sessionStorage.setItem(key, JSON.stringify(payload));
    }
    catch (_err) {
        // Ignore storage errors; cache is best-effort.
    }
}
function loadSessionCache(key, maxAgeMs) {
    try {
        const raw = sessionStorage.getItem(key);
        if (!raw)
            return null;
        const payload = JSON.parse(raw);
        if (!payload || typeof payload.at !== "number")
            return null;
        if (maxAgeMs && Date.now() - payload.at > maxAgeMs)
            return null;
        return payload.value;
    }
    catch (_err) {
        return null;
    }
}
function saveHubViewPrefs() {
    try {
        localStorage.setItem(HUB_VIEW_PREFS_KEY, JSON.stringify(hubViewPrefs));
    }
    catch (_err) {
        // Ignore local storage failures; prefs are best-effort.
    }
}
function saveHubOpenPanel(value) {
    try {
        localStorage.setItem(HUB_PANEL_PREFS_KEY, value);
    }
    catch (_err) {
        // Ignore local storage failures; prefs are best-effort.
    }
}
function loadHubOpenPanel() {
    try {
        const raw = localStorage.getItem(HUB_PANEL_PREFS_KEY);
        if (raw === "repos" || raw === "agents") {
            return raw;
        }
    }
    catch (_err) {
        // Ignore parse/storage errors; defaults apply.
    }
    return "repos";
}
function loadHubViewPrefs() {
    try {
        const raw = localStorage.getItem(HUB_VIEW_PREFS_KEY);
        if (!raw)
            return;
        const parsed = JSON.parse(raw);
        const flowFilter = parsed.flowFilter;
        const sortOrder = parsed.sortOrder;
        if (flowFilter === "all" ||
            flowFilter === "active" ||
            flowFilter === "running" ||
            flowFilter === "paused" ||
            flowFilter === "completed" ||
            flowFilter === "failed" ||
            flowFilter === "idle") {
            hubViewPrefs.flowFilter = flowFilter;
        }
        if (sortOrder === "repo_id" ||
            sortOrder === "last_activity_desc" ||
            sortOrder === "last_activity_asc" ||
            sortOrder === "flow_progress_desc") {
            hubViewPrefs.sortOrder = sortOrder;
        }
    }
    catch (_err) {
        // Ignore parse/storage errors; defaults apply.
    }
}
function normalizePinnedParentRepoIds(value) {
    if (!Array.isArray(value))
        return [];
    const out = [];
    const seen = new Set();
    value.forEach((entry) => {
        if (typeof entry !== "string")
            return;
        const repoId = entry.trim();
        if (!repoId || seen.has(repoId))
            return;
        seen.add(repoId);
        out.push(repoId);
    });
    return out;
}
function formatRunSummary(repo) {
    if (!repo.initialized)
        return "Not initialized";
    if (!repo.exists_on_disk)
        return "Missing on disk";
    if (!repo.last_run_id)
        return "No runs yet";
    const exit = repo.last_exit_code === null || repo.last_exit_code === undefined
        ? ""
        : ` exit:${repo.last_exit_code}`;
    return `#${repo.last_run_id}${exit}`;
}
function formatLastActivity(repo) {
    if (!repo.initialized)
        return "";
    const time = repo.last_run_finished_at || repo.last_run_started_at;
    if (!time)
        return "";
    return formatTimeCompact(time);
}
function formatRunDuration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
        return "";
    }
    const rounded = Math.max(0, Math.floor(seconds));
    if (rounded < 60)
        return `${rounded}s`;
    const minutes = Math.floor(rounded / 60);
    const remainingSeconds = rounded % 60;
    if (minutes < 60) {
        return remainingSeconds === 0 ? `${minutes}m` : `${minutes}m ${remainingSeconds}s`;
    }
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    if (hours < 24) {
        return remainingMinutes === 0 ? `${hours}h` : `${hours}h ${remainingMinutes}m`;
    }
    const days = Math.floor(hours / 24);
    const remainingHours = hours % 24;
    return remainingHours === 0 ? `${days}d` : `${days}d ${remainingHours}h`;
}
function formatFreshnessAge(ageSeconds) {
    if (typeof ageSeconds !== "number" || !Number.isFinite(ageSeconds) || ageSeconds < 0) {
        return "";
    }
    if (ageSeconds < 60)
        return `${Math.floor(ageSeconds)}s`;
    if (ageSeconds < 3600)
        return `${Math.floor(ageSeconds / 60)}m`;
    if (ageSeconds < 86400)
        return `${Math.floor(ageSeconds / 3600)}h`;
    return `${Math.floor(ageSeconds / 86400)}d`;
}
function freshnessBasisLabel(raw) {
    const value = String(raw || "").trim();
    if (!value)
        return "snapshot";
    return value
        .replace(/_/g, " ")
        .replace(/\bat\b/g, "")
        .trim();
}
function freshnessSummary(freshness) {
    if (!freshness)
        return "";
    const basis = freshnessBasisLabel(freshness.recency_basis);
    const age = formatFreshnessAge(freshness.age_seconds);
    if (basis && age)
        return `${basis} ${age} ago`;
    if (age)
        return `${age} old`;
    if (basis)
        return basis;
    return "";
}
function repoFreshness(repo) {
    const extendedRepo = repo;
    return extendedRepo.canonical_state_v1?.freshness || null;
}
function formatDestinationSummary(destination) {
    if (!destination || typeof destination !== "object")
        return "local";
    const kindRaw = destination.kind;
    const kind = typeof kindRaw === "string" ? kindRaw.trim().toLowerCase() : "local";
    if (kind === "docker") {
        const image = typeof destination.image === "string" ? destination.image.trim() : "";
        return image ? `docker:${image}` : "docker";
    }
    return "local";
}
function splitCommaSeparated(value) {
    return value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
}
function currentDockerEnvPassthrough(destination) {
    const raw = destination?.env_passthrough;
    if (!Array.isArray(raw))
        return "";
    return raw
        .map((item) => String(item || "").trim())
        .filter(Boolean)
        .join(", ");
}
function currentDockerProfile(destination) {
    return typeof destination?.profile === "string"
        ? String(destination.profile).trim()
        : "";
}
function currentDockerWorkdir(destination) {
    return typeof destination?.workdir === "string"
        ? String(destination.workdir).trim()
        : "";
}
function currentDockerExplicitEnv(destination) {
    const raw = destination?.env;
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
        return "";
    return Object.entries(raw)
        .map(([key, value]) => {
        const cleanKey = String(key || "").trim();
        if (!cleanKey)
            return "";
        if (value === null || value === undefined)
            return "";
        return `${cleanKey}=${String(value)}`;
    })
        .filter(Boolean)
        .join(", ");
}
function currentDockerMounts(destination) {
    const raw = destination?.mounts;
    if (!Array.isArray(raw))
        return "";
    const mounts = raw
        .map((item) => {
        if (!item || typeof item !== "object")
            return "";
        const source = String(item.source || "").trim();
        const target = String(item.target || "").trim();
        const rawReadOnly = item.read_only ??
            item.readOnly ??
            item.readonly;
        const readOnly = rawReadOnly === true;
        if (!source || !target)
            return "";
        return readOnly ? `${source}:${target}:ro` : `${source}:${target}`;
    })
        .filter(Boolean);
    return mounts.join(", ");
}
function parseDockerEnvMap(value) {
    const env = {};
    const entries = splitCommaSeparated(value);
    for (const entry of entries) {
        const splitAt = entry.indexOf("=");
        if (splitAt <= 0) {
            return {
                env: {},
                error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
            };
        }
        const key = entry.slice(0, splitAt).trim();
        const mapValue = entry.slice(splitAt + 1);
        if (!key) {
            return {
                env: {},
                error: `Invalid env entry "${entry}". Use KEY=VALUE (comma-separated).`,
            };
        }
        env[key] = mapValue;
    }
    return { env, error: null };
}
function parseDockerMountList(value) {
    const mounts = [];
    const entries = splitCommaSeparated(value);
    for (const entry of entries) {
        let mountSpec = entry;
        let readOnly = null;
        const lowerEntry = entry.toLowerCase();
        if (lowerEntry.endsWith(":ro")) {
            mountSpec = entry.slice(0, -3);
            readOnly = true;
        }
        else if (lowerEntry.endsWith(":rw")) {
            mountSpec = entry.slice(0, -3);
            readOnly = false;
        }
        const splitAt = mountSpec.lastIndexOf(":");
        if (splitAt <= 0 || splitAt >= mountSpec.length - 1) {
            return {
                mounts: [],
                error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
            };
        }
        const source = mountSpec.slice(0, splitAt).trim();
        const target = mountSpec.slice(splitAt + 1).trim();
        if (!source || !target) {
            return {
                mounts: [],
                error: `Invalid mount "${entry}". Use source:target[:ro] (comma-separated).`,
            };
        }
        if (readOnly === true) {
            mounts.push({ source, target, read_only: true });
        }
        else {
            mounts.push({ source, target });
        }
    }
    return { mounts, error: null };
}
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
function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
async function pollHubJob(jobId, { timeoutMs = HUB_JOB_TIMEOUT_MS } = {}) {
    const start = Date.now();
    for (;;) {
        const job = await api(`/hub/jobs/${jobId}`, { method: "GET" });
        if (job.status === "succeeded")
            return job;
        if (job.status === "failed") {
            const err = job.error || "Hub job failed";
            throw new Error(err);
        }
        if (Date.now() - start > timeoutMs) {
            throw new Error("Hub job timed out");
        }
        await sleep(HUB_JOB_POLL_INTERVAL_MS);
    }
}
function updateCleanupAllThreadsButton(repos) {
    const btn = getHubCleanupAllThreadsBtn();
    if (!btn)
        return;
    const cleanupRepos = baseReposWithUnboundThreads(repos);
    const totalThreads = totalUnboundManagedThreadCount(cleanupRepos);
    const dirtyRepos = dirtyBaseReposWithUnboundThreads(repos);
    const empty = totalThreads <= 0;
    btn.textContent =
        totalThreads > 0 ? `Cleanup all (${totalThreads})` : "Cleanup all";
    if (cleanupAllRepoThreadsInFlight) {
        btn.disabled = true;
        btn.classList.remove("hub-cleanup-all--empty");
        btn.removeAttribute("aria-disabled");
        btn.title = "Cleaning up stale threads…";
        return;
    }
    // Do not use the native `disabled` attribute when empty: it swallows clicks
    // with no feedback; use aria + styling so the handler can flash a toast.
    btn.disabled = false;
    if (empty) {
        btn.setAttribute("aria-disabled", "true");
    }
    else {
        btn.removeAttribute("aria-disabled");
    }
    btn.classList.toggle("hub-cleanup-all--empty", empty);
    if (totalThreads <= 0) {
        btn.title =
            "No stale non-chat-bound managed threads across base repos (click for confirmation)";
        return;
    }
    const dirtySummary = dirtyRepos.length
        ? ` Includes ${dirtyRepos.length} dirty repo${dirtyRepos.length === 1 ? "" : "s"}.`
        : "";
    btn.title =
        `Archive ${totalThreads} stale non-chat-bound managed thread${totalThreads === 1 ? "" : "s"} across ${cleanupRepos.length} base repo${cleanupRepos.length === 1 ? "" : "s"}.` +
            dirtySummary;
}
async function startHubJob(path, { body, startedMessage } = {}) {
    const job = await api(path, { method: "POST", body });
    if (startedMessage) {
        flash(startedMessage);
    }
    return pollHubJob(job.job_id);
}
function formatTimeCompact(isoString) {
    if (!isoString)
        return "–";
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime()))
        return isoString;
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1)
        return "just now";
    if (mins < 60)
        return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24)
        return `${hours}h ago`;
    return date.toLocaleDateString();
}
function renderSummary(repos) {
    const running = repos.filter((r) => r.status === "running").length;
    const missing = repos.filter((r) => !r.exists_on_disk).length;
    updateCleanupAllThreadsButton(repos);
    if (totalEl)
        totalEl.textContent = repos.length.toString();
    if (runningEl)
        runningEl.textContent = running.toString();
    if (missingEl)
        missingEl.textContent = missing.toString();
    if (lastScanEl) {
        lastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
    }
    if (pmaLastScanEl) {
        pmaLastScanEl.textContent = formatTimeCompact(hubData.last_scan_at);
    }
}
function formatTokensCompact(val) {
    if (val === null || val === undefined)
        return "0";
    const num = Number(val);
    if (Number.isNaN(num))
        return String(val);
    if (num >= 1000000)
        return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000)
        return `${(num / 1000).toFixed(0)}k`;
    return num.toLocaleString();
}
function getRepoUsage(repoId) {
    const usage = hubUsageIndex[repoId];
    if (!usage)
        return { label: "—", hasData: false };
    const totals = usage.totals || {};
    return {
        label: formatTokensCompact(totals.total_tokens),
        hasData: true,
    };
}
function indexHubUsage(data) {
    hubUsageIndex = {};
    hubUsageUnmatched = data?.unmatched || null;
    if (!data?.repos)
        return;
    data.repos.forEach((repo) => {
        if (repo?.id)
            hubUsageIndex[repo.id] = repo;
    });
}
function renderHubUsageMeta(data) {
    if (hubUsageMeta) {
        hubUsageMeta.textContent = data?.codex_home || "–";
    }
}
function scheduleHubUsageSummaryRetry() {
    clearHubUsageSummaryRetry();
    hubUsageSummaryRetryTimer = setTimeout(() => {
        loadHubUsage();
    }, 1500);
}
function clearHubUsageSummaryRetry() {
    if (hubUsageSummaryRetryTimer) {
        clearTimeout(hubUsageSummaryRetryTimer);
        hubUsageSummaryRetryTimer = null;
    }
}
function handleHubUsagePayload(data, { cachedUsage, allowRetry }) {
    const hasSummary = data && Array.isArray(data.repos);
    const effective = hasSummary ? data : cachedUsage;
    if (effective) {
        indexHubUsage(effective);
        renderHubUsageMeta(effective);
        renderReposWithScroll(hubData.repos || []);
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
        renderReposWithScroll(hubData.repos || []);
    }
    return false;
}
async function loadHubUsage({ silent = false, allowRetry = true } = {}) {
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
async function loadUpdateTargetOptions(selectId) {
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
async function handleSystemUpdate(btnId, targetSelectId) {
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
function initHubSettings() {
    const settingsBtns = Array.from(document.querySelectorAll("#hub-settings, #pma-settings"));
    const modal = document.getElementById("hub-settings-modal");
    const closeBtn = document.getElementById("hub-settings-close");
    const updateBtn = document.getElementById("hub-update-btn");
    const updateTarget = document.getElementById("hub-update-target");
    void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
    let closeModal = null;
    const hideModal = () => {
        if (closeModal) {
            const close = closeModal;
            closeModal = null;
            close();
        }
    };
    if (modal && settingsBtns.length > 0) {
        settingsBtns.forEach((settingsBtn) => {
            settingsBtn.addEventListener("click", () => {
                const triggerEl = document.activeElement;
                hideModal();
                closeModal = openModal(modal, {
                    initialFocus: closeBtn || updateBtn || modal,
                    returnFocusTo: triggerEl,
                    onRequestClose: hideModal,
                });
            });
        });
    }
    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => {
            hideModal();
        });
    }
    if (updateBtn) {
        updateBtn.addEventListener("click", () => handleSystemUpdate("hub-update-btn", updateTarget ? updateTarget.id : null));
    }
}
function buildActions(repo) {
    const actions = [];
    const missing = !repo.exists_on_disk;
    const kind = repo.kind || "base";
    const unboundThreads = unboundManagedThreadCount(repo);
    if (!missing && repo.mount_error) {
        actions.push({ key: "init", label: "Retry mount", kind: "primary" });
    }
    else if (!missing && repo.init_error) {
        actions.push({
            key: "init",
            label: repo.initialized ? "Re-init" : "Init",
            kind: "primary",
        });
    }
    else if (!missing && !repo.initialized) {
        actions.push({ key: "init", label: "Init", kind: "primary" });
    }
    if (kind === "base") {
        actions.push({
            key: "repo_settings",
            label: "Settings",
            kind: "ghost",
            title: "Repository settings",
        });
    }
    if (!missing && (repo.has_car_state || (kind === "base" && unboundThreads > 0))) {
        actions.push({
            key: "archive_state",
            label: "Archive",
            kind: "ghost",
            title: kind === "base" && unboundThreads > 0
                ? `Archive CAR state and ${unboundThreads} repo-linked managed thread${unboundThreads === 1 ? "" : "s"} for fresh work`
                : "Archive CAR runtime state and reset this workspace for fresh work",
        });
    }
    if (!missing && kind === "base") {
        actions.push({ key: "new_worktree", label: "New Worktree", kind: "ghost" });
        const clean = repo.is_clean;
        const syncDisabled = clean !== true;
        const syncTitle = syncDisabled
            ? "Working tree must be clean to sync main"
            : "Switch to main and pull latest";
        actions.push({
            key: "sync_main",
            label: "Sync main",
            kind: "ghost",
            title: syncTitle,
            disabled: syncDisabled,
        });
    }
    if (!missing && kind === "worktree") {
        const cleanupBlockedByChatBinding = isCleanupBlockedByChatBinding(repo);
        actions.push({
            key: "cleanup_worktree",
            label: "Cleanup",
            kind: "ghost",
            title: cleanupBlockedByChatBinding
                ? "Unbind Discord/Telegram chats before cleanup"
                : "Remove worktree and delete branch",
            disabled: cleanupBlockedByChatBinding,
        });
    }
    return actions;
}
function buildAgentWorkspaceActions(workspace) {
    return [
        {
            key: workspace.enabled ? "disable" : "enable",
            label: workspace.enabled ? "Disable" : "Enable",
            kind: workspace.enabled ? "ghost" : "primary",
            title: workspace.enabled
                ? "Disable this agent workspace"
                : "Enable this agent workspace",
        },
        {
            key: "set_destination",
            label: "Destination",
            kind: "ghost",
            title: "Set agent workspace destination",
        },
        {
            key: "remove",
            label: "Remove",
            kind: "ghost",
            title: "Unregister this workspace but keep managed files",
        },
        {
            key: "delete",
            label: "Delete",
            kind: "danger",
            title: "Unregister and delete the managed workspace directory",
        },
    ];
}
async function openRepoSettingsModal(repo) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog repo-settings-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const header = document.createElement("div");
    header.className = "modal-header";
    const title = document.createElement("span");
    title.className = "label";
    title.textContent = `Settings: ${repo.display_name || repo.id}`;
    header.appendChild(title);
    const body = document.createElement("div");
    body.className = "modal-body";
    const worktreeSection = document.createElement("div");
    worktreeSection.className = "form-group";
    const worktreeLabel = document.createElement("label");
    worktreeLabel.textContent = "Worktree Setup Commands";
    const worktreeHint = document.createElement("p");
    worktreeHint.className = "muted small";
    worktreeHint.textContent =
        "Commands run with /bin/sh -lc after creating a new worktree. One per line, leave blank to disable.";
    const textarea = document.createElement("textarea");
    textarea.rows = 4;
    textarea.style.width = "100%";
    textarea.style.resize = "vertical";
    textarea.placeholder = "make setup\npnpm install\npre-commit install";
    textarea.value = (repo.worktree_setup_commands || []).join("\n");
    worktreeSection.append(worktreeLabel, worktreeHint, textarea);
    body.appendChild(worktreeSection);
    const destinationSection = document.createElement("div");
    destinationSection.className = "form-group";
    const destinationLabel = document.createElement("label");
    destinationLabel.textContent = "Execution Destination";
    const destinationHint = document.createElement("p");
    destinationHint.className = "muted small";
    destinationHint.textContent = "Set where runs execute for this repo.";
    const destinationRow = document.createElement("div");
    destinationRow.className = "settings-actions";
    const destinationPill = document.createElement("span");
    destinationPill.className = "pill pill-small hub-destination-settings-pill";
    destinationPill.textContent = formatDestinationSummary(repo.effective_destination);
    const destinationBtn = document.createElement("button");
    destinationBtn.className = "ghost";
    destinationBtn.textContent = "Change destination";
    destinationRow.append(destinationPill, destinationBtn);
    destinationSection.append(destinationLabel, destinationHint, destinationRow);
    body.appendChild(destinationSection);
    const dangerSection = document.createElement("div");
    dangerSection.className = "form-group settings-section-danger";
    const dangerLabel = document.createElement("label");
    dangerLabel.textContent = "Danger Zone";
    const dangerHint = document.createElement("p");
    dangerHint.className = "muted small";
    dangerHint.textContent =
        "Remove this repo from hub and delete its local directory.";
    const removeBtn = document.createElement("button");
    removeBtn.className = "danger sm";
    removeBtn.textContent = "Remove repo";
    dangerSection.append(dangerLabel, dangerHint, removeBtn);
    body.appendChild(dangerSection);
    const footer = document.createElement("div");
    footer.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "ghost";
    cancelBtn.textContent = "Cancel";
    const saveBtn = document.createElement("button");
    saveBtn.className = "primary";
    saveBtn.textContent = "Save";
    footer.append(cancelBtn, saveBtn);
    dialog.append(header, body, footer);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    return new Promise((resolve) => {
        let closeModal = null;
        let settled = false;
        const finalize = async (action) => {
            if (settled)
                return;
            settled = true;
            if (closeModal) {
                const close = closeModal;
                closeModal = null;
                close();
            }
            overlay.remove();
            if (action === "save") {
                const commands = textarea.value
                    .split("\n")
                    .map((line) => line.trim())
                    .filter(Boolean);
                try {
                    await api(`/hub/repos/${encodeURIComponent(repo.id)}/worktree-setup`, {
                        method: "POST",
                        body: { commands },
                    });
                    flash(commands.length
                        ? `Saved ${commands.length} setup command(s) for ${repo.id}`
                        : `Cleared setup commands for ${repo.id}`, "success");
                    await refreshHub();
                }
                catch (err) {
                    flash(err.message || "Failed to save settings", "error");
                }
            }
            if (action === "destination") {
                try {
                    const updated = await promptAndSetRepoDestination(repo);
                    if (updated) {
                        await refreshHub();
                    }
                }
                catch (err) {
                    flash(err.message || "Failed to update destination", "error");
                }
            }
            if (action === "remove") {
                try {
                    await removeRepoWithChecks(repo.id);
                }
                catch (err) {
                    flash(err.message || "Failed to remove repo", "error");
                }
            }
            resolve();
        };
        closeModal = openModal(overlay, {
            initialFocus: textarea,
            returnFocusTo: document.activeElement,
            onRequestClose: () => finalize("cancel"),
            onKeydown: (event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                    event.preventDefault();
                    finalize("save");
                }
            },
        });
        cancelBtn.addEventListener("click", () => finalize("cancel"));
        saveBtn.addEventListener("click", () => finalize("save"));
        destinationBtn.addEventListener("click", () => finalize("destination"));
        removeBtn.addEventListener("click", () => finalize("remove"));
    });
}
function buildDestinationBadge(destination) {
    const summary = formatDestinationSummary(destination);
    const isDocker = summary.startsWith("docker");
    const label = isDocker ? "docker" : "local";
    const titleAttr = summary !== label ? ` title="${escapeHtml(summary)}"` : "";
    const className = isDocker
        ? "pill pill-small pill-info hub-destination-pill hub-destination-pill-docker"
        : "pill pill-small pill-info hub-destination-pill";
    return `<span class="${className}"${titleAttr}>${escapeHtml(label)}</span>`;
}
function buildFlowStatusBadge(statusLabel, statusValue) {
    const normalized = String(statusValue || "idle").toLowerCase();
    if (["idle", "completed", "success", "ready"].includes(normalized)) {
        return "";
    }
    return `<span class="pill pill-small hub-status-pill">${escapeHtml(statusLabel)}</span>`;
}
function buildMountBadge(repo) {
    if (!repo)
        return "";
    const missing = !repo.exists_on_disk;
    let label;
    let className = "pill pill-small";
    let title;
    if (missing) {
        label = "missing";
        className += " pill-error";
        title = "Repo path not found on disk";
    }
    else if (repo.mount_error) {
        label = "mount error";
        className += " pill-error";
        title = repo.mount_error;
    }
    else if (repo.mounted !== true) {
        label = "not mounted";
        className += " pill-warn";
    }
    else {
        return "";
    }
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
    return `<span class="${className} hub-mount-pill"${titleAttr}>${escapeHtml(label)}</span>`;
}
function inferBaseId(repo) {
    if (!repo)
        return null;
    if (repo.worktree_of)
        return repo.worktree_of;
    if (typeof repo.id === "string" && repo.id.includes("--")) {
        return repo.id.split("--")[0];
    }
    return null;
}
function repoLastActivityMs(repo) {
    const raw = repo.last_run_finished_at || repo.last_run_started_at;
    if (!raw)
        return 0;
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? 0 : parsed;
}
function repoFlowStatus(repo) {
    const status = repo.ticket_flow_display?.status || repo.ticket_flow?.status || "idle";
    return String(status || "idle").toLowerCase();
}
function repoFlowProgress(repo) {
    const done = Number(repo.ticket_flow_display?.done_count || repo.ticket_flow?.done_count || 0);
    const total = Number(repo.ticket_flow_display?.total_count || repo.ticket_flow?.total_count || 0);
    if (total <= 0)
        return 0;
    return done / total;
}
function repoMatchesFlowFilter(repo, filter) {
    if (filter === "all")
        return true;
    const flowStatus = repoFlowStatus(repo);
    if (filter === "active") {
        return (flowStatus === "running" ||
            flowStatus === "pending" ||
            flowStatus === "paused" ||
            flowStatus === "stopping");
    }
    if (filter === "running")
        return flowStatus === "running";
    if (filter === "paused")
        return flowStatus === "paused";
    if (filter === "completed")
        return flowStatus === "completed" || flowStatus === "done";
    if (filter === "failed") {
        return (flowStatus === "failed" ||
            flowStatus === "stopped" ||
            flowStatus === "superseded");
    }
    return flowStatus === "idle";
}
function compareReposForSort(a, b, sortOrder) {
    if (sortOrder === "last_activity_desc") {
        return (repoLastActivityMs(b) - repoLastActivityMs(a) ||
            String(a.id).localeCompare(String(b.id)));
    }
    if (sortOrder === "last_activity_asc") {
        return (repoLastActivityMs(a) - repoLastActivityMs(b) ||
            String(a.id).localeCompare(String(b.id)));
    }
    if (sortOrder === "flow_progress_desc") {
        return (repoFlowProgress(b) - repoFlowProgress(a) ||
            repoLastActivityMs(b) - repoLastActivityMs(a) ||
            String(a.id).localeCompare(String(b.id)));
    }
    return String(a.id).localeCompare(String(b.id));
}
function normalizedHubSearch() {
    return String(hubRepoSearchInput?.value || "").trim().toLowerCase();
}
function repoSearchBlob(repo) {
    const status = repo.ticket_flow_display?.status_label || repo.ticket_flow_display?.status || repo.status;
    const destination = formatDestinationSummary(repo.effective_destination);
    const parts = [
        repo.id,
        repo.display_name,
        repo.path,
        repo.status,
        status,
        repo.lock_status,
        repo.kind,
        repo.worktree_of,
        repo.branch,
        destination,
        repo.mount_error,
        repo.init_error,
    ].filter(Boolean);
    return parts.join(" ").toLowerCase();
}
function repoMatchesSearch(repo, query) {
    if (!query)
        return true;
    return repoSearchBlob(repo).includes(query);
}
function channelSearchBlob(channel) {
    const parts = [
        channel.key,
        channel.display,
        channel.source,
        channel.repo_id,
        channel.resource_kind,
        channel.resource_id,
        channel.status_label || channel.channel_status,
        channel.workspace_path,
        JSON.stringify(channel.meta || {}),
        JSON.stringify(channel.provenance || {}),
    ];
    return parts
        .map((part) => String(part || ""))
        .join(" ")
        .toLowerCase();
}
function channelMatchesSearch(channel, query) {
    if (!query)
        return true;
    return channelSearchBlob(channel).includes(query);
}
function toPositiveInt(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0)
        return null;
    return Math.floor(parsed);
}
function channelSource(channel) {
    const raw = String(channel.source || channel.provenance?.source || channel.entry?.platform || "")
        .trim()
        .toLowerCase();
    if (raw === "discord" || raw === "telegram" || raw === "pma_thread") {
        return raw;
    }
    return "unknown";
}
function channelSourceBadgeLabel(channel) {
    const source = channelSource(channel);
    if (source === "discord")
        return "Discord";
    if (source === "telegram")
        return "Telegram";
    if (source === "pma_thread")
        return "PMA";
    return "Unknown";
}
function channelSourceBadgeClass(channel) {
    const source = channelSource(channel);
    if (source === "discord")
        return "discord";
    if (source === "telegram")
        return "telegram";
    if (source === "pma_thread")
        return "pma";
    return "unknown";
}
function channelSourceBadgeMarkup(channel) {
    return `<span class="pill pill-small hub-chat-binding-source hub-chat-binding-source-${escapeHtml(channelSourceBadgeClass(channel))}">${escapeHtml(channelSourceBadgeLabel(channel))}</span>`;
}
function channelPmaDetails(channel) {
    if (channelSource(channel) !== "pma_thread")
        return "";
    const parts = [];
    const agent = String(channel.provenance?.agent || channel.meta?.agent || "")
        .trim()
        .toLowerCase();
    if (agent) {
        parts.push(`agent ${agent}`);
    }
    const managedId = String(channel.provenance?.managed_thread_id || channel.active_thread_id || "").trim();
    if (managedId) {
        parts.push(`thread ${managedId.slice(0, 12)}`);
    }
    const reason = String(channel.provenance?.status_reason_code || channel.meta?.status_reason_code || "").trim();
    if (reason) {
        parts.push(reason);
    }
    return parts.join(" · ");
}
function isManagedPmaChannel(channel) {
    return channelSource(channel) === "pma_thread";
}
function rawChannelDisplayLabel(channel) {
    if (typeof channel.display === "string" && channel.display.trim()) {
        return channel.display.trim();
    }
    return channel.key;
}
function titleCaseWord(value) {
    if (!value)
        return value;
    return value.charAt(0).toUpperCase() + value.slice(1);
}
function channelDisplayLabel(channel) {
    const rawLabel = rawChannelDisplayLabel(channel);
    if (channelSource(channel) !== "pma_thread") {
        return rawLabel;
    }
    const normalizedRaw = rawLabel.trim().toLowerCase();
    const threadKind = String(channel.provenance?.thread_kind || channel.meta?.thread_kind || "")
        .trim()
        .toLowerCase();
    if (threadKind === "ticket_flow" ||
        normalizedRaw === "ticket-flow" ||
        normalizedRaw.startsWith("ticket-flow:")) {
        return "Ticket flow";
    }
    if (normalizedRaw.startsWith("pma:") ||
        normalizedRaw === "pma" ||
        threadKind === "interactive") {
        const agent = String(channel.provenance?.agent || channel.meta?.agent || "")
            .trim()
            .toLowerCase();
        return agent ? `${titleCaseWord(agent)} thread` : "Agent thread";
    }
    if (normalizedRaw.startsWith("discord:")) {
        return "Discord thread";
    }
    if (normalizedRaw.startsWith("telegram:")) {
        return "Telegram thread";
    }
    return rawLabel;
}
function channelOwnerSummary(channel) {
    const resourceKind = String(channel.resource_kind || channel.provenance?.resource_kind || "")
        .trim()
        .toLowerCase();
    const resourceId = String(channel.resource_id || channel.provenance?.resource_id || "").trim();
    if (resourceKind === "agent_workspace" && resourceId) {
        return `agent workspace ${resourceId}`;
    }
    if (typeof channel.repo_id === "string" && channel.repo_id.trim()) {
        return `repo ${channel.repo_id.trim()}`;
    }
    return "owner unbound";
}
function channelMetaSummary(channel, { includeRepo = true } = {}) {
    const parts = [];
    const pmaDetails = channelPmaDetails(channel);
    const status = String(channel.status_label || channel.channel_status || "unknown")
        .trim()
        .toLowerCase();
    parts.push(status || "unknown");
    if (pmaDetails) {
        parts.push(pmaDetails);
    }
    if (channel.seen_at) {
        parts.push(`seen ${formatTimeCompact(channel.seen_at)}`);
    }
    const totalTokens = toPositiveInt(channel.token_usage?.total_tokens);
    if (totalTokens !== null) {
        parts.push(`tok ${formatTokensCompact(totalTokens)}`);
    }
    const insertions = toPositiveInt(channel.diff_stats?.insertions) || 0;
    const deletions = toPositiveInt(channel.diff_stats?.deletions) || 0;
    const filesChanged = toPositiveInt(channel.diff_stats?.files_changed);
    if (insertions || deletions || filesChanged) {
        let diffPart = `+${insertions}/-${deletions}`;
        if (filesChanged) {
            diffPart += ` · f${filesChanged}`;
        }
        parts.push(diffPart);
    }
    if (includeRepo) {
        parts.push(channelOwnerSummary(channel));
    }
    return parts.join(" · ");
}
function channelSummarySubline(channel, { lastActivity = "", additionalCount = 0, } = {}) {
    const label = channelDisplayLabel(channel);
    const additionalMarkup = additionalCount > 0
        ? `<span class="hub-chat-binding-more muted small">+${additionalCount} more</span>`
        : "";
    const activityMarkup = lastActivity
        ? `<span class="muted small">·</span><span class="hub-repo-info-line">${escapeHtml(lastActivity)}</span>`
        : "";
    return `<div class="hub-repo-subline hub-chat-binding-summary">
    ${channelSourceBadgeMarkup(channel)}
    <span class="hub-chat-binding-label">${escapeHtml(label)}</span>
    ${additionalMarkup}
    ${activityMarkup}
  </div>`;
}
function pmaSummaryMarkup(channel, { lastActivity = "", label = channelDisplayLabel(channel), count = 1, } = {}) {
    const latestSeenAt = typeof channel.seen_at === "string" && channel.seen_at
        ? formatTimeCompact(channel.seen_at)
        : "";
    const metaParts = [];
    if (latestSeenAt) {
        metaParts.push(`seen ${latestSeenAt}`);
    }
    if (lastActivity) {
        metaParts.push(lastActivity);
    }
    const countMarkup = count > 1
        ? `<span class="hub-chat-binding-count">x${escapeHtml(String(count))}</span>`
        : "";
    return `
    <div class="hub-chat-binding-row hub-chat-binding-row-compact">
      <div class="hub-chat-binding-main">
        ${channelSourceBadgeMarkup(channel)}
        <span class="hub-chat-binding-label">${escapeHtml(label)}</span>
        ${countMarkup}
      </div>
      <div class="hub-chat-binding-meta muted small">${escapeHtml(metaParts.join(" · "))}</div>
    </div>
  `;
}
function pmaChannelGroupKey(channel) {
    const threadKind = String(channel.provenance?.thread_kind || channel.meta?.thread_kind || "")
        .trim()
        .toLowerCase();
    const display = rawChannelDisplayLabel(channel);
    const normalizedDisplay = display.trim().toLowerCase();
    if (threadKind === "ticket_flow" ||
        normalizedDisplay === "ticket-flow" ||
        normalizedDisplay.startsWith("ticket-flow:")) {
        return "ticket-flow";
    }
    return `display:${normalizedDisplay}`;
}
function pmaChannelGroupLabel(channel) {
    const key = pmaChannelGroupKey(channel);
    if (key === "ticket-flow")
        return "Ticket flow";
    return channelDisplayLabel(channel);
}
function isDuplicateChatBoundPmaChannel(channel, visibleChannels) {
    const normalizedLabel = rawChannelDisplayLabel(channel).trim().toLowerCase();
    if (!normalizedLabel ||
        (!normalizedLabel.startsWith("discord:") &&
            !normalizedLabel.startsWith("telegram:"))) {
        return false;
    }
    return visibleChannels.some((channel) => {
        const channelKey = String(channel.key || "")
            .trim()
            .toLowerCase();
        return (channelKey === normalizedLabel ||
            channelKey.startsWith(`${normalizedLabel}:`) ||
            normalizedLabel.startsWith(`${channelKey}:`));
    });
}
function groupPmaChannels(channels) {
    const grouped = new Map();
    channels.forEach((channel) => {
        const key = pmaChannelGroupKey(channel);
        const existing = grouped.get(key);
        if (existing) {
            existing.count += 1;
            if (channelSeenAtMs(channel) > channelSeenAtMs(existing.latest)) {
                existing.latest = channel;
            }
            return;
        }
        grouped.set(key, {
            label: pmaChannelGroupLabel(channel),
            count: 1,
            latest: channel,
        });
    });
    return Array.from(grouped.values()).sort((a, b) => {
        const seenDiff = channelSeenAtMs(b.latest) - channelSeenAtMs(a.latest);
        if (seenDiff !== 0)
            return seenDiff;
        return a.label.localeCompare(b.label);
    });
}
function channelSeenAtMs(channel) {
    if (!channel.seen_at)
        return 0;
    const parsed = Date.parse(channel.seen_at);
    return Number.isNaN(parsed) ? 0 : parsed;
}
function channelsByRepoId(entries) {
    const byRepo = new Map();
    entries.forEach((entry) => {
        const repoId = String(entry.repo_id || "").trim();
        if (!repoId)
            return;
        if (!byRepo.has(repoId)) {
            byRepo.set(repoId, []);
        }
        byRepo.get(repoId).push(entry);
    });
    byRepo.forEach((repoEntries) => {
        repoEntries.sort((a, b) => {
            const seenDiff = channelSeenAtMs(b) - channelSeenAtMs(a);
            if (seenDiff !== 0)
                return seenDiff;
            return channelDisplayLabel(a).localeCompare(channelDisplayLabel(b));
        });
    });
    return byRepo;
}
function channelsByAgentWorkspaceId(entries) {
    const byWorkspace = new Map();
    entries.forEach((entry) => {
        const resourceKind = String(entry.resource_kind || "").trim().toLowerCase();
        const resourceId = String(entry.resource_id || "").trim();
        if (resourceKind !== "agent_workspace" || !resourceId)
            return;
        if (!byWorkspace.has(resourceId)) {
            byWorkspace.set(resourceId, []);
        }
        byWorkspace.get(resourceId).push(entry);
    });
    byWorkspace.forEach((workspaceEntries) => {
        workspaceEntries.sort((a, b) => {
            const seenDiff = channelSeenAtMs(b) - channelSeenAtMs(a);
            if (seenDiff !== 0)
                return seenDiff;
            return channelDisplayLabel(a).localeCompare(channelDisplayLabel(b));
        });
    });
    return byWorkspace;
}
function buildRepoGroups(repos) {
    const bases = repos.filter((r) => (r.kind || "base") === "base");
    const allWorktrees = repos.filter((r) => (r.kind || "base") === "worktree");
    const chatBoundWorktrees = [];
    const worktrees = [];
    allWorktrees.forEach((repo) => {
        if (isChatBoundWorktree(repo)) {
            chatBoundWorktrees.push(repo);
            return;
        }
        worktrees.push(repo);
    });
    const byBase = new Map();
    bases.forEach((b) => byBase.set(b.id, { base: b, worktrees: [] }));
    const orphanWorktrees = [];
    worktrees.forEach((w) => {
        const baseId = inferBaseId(w);
        if (baseId && byBase.has(baseId)) {
            byBase.get(baseId).worktrees.push(w);
        }
        else {
            orphanWorktrees.push(w);
        }
    });
    const groups = [...byBase.values()].map((group) => {
        const filteredWorktrees = hubViewPrefs.flowFilter === "all"
            ? [...group.worktrees]
            : group.worktrees.filter((repo) => repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter));
        const baseMatches = repoMatchesFlowFilter(group.base, hubViewPrefs.flowFilter);
        const matchesFilter = hubViewPrefs.flowFilter === "all" || baseMatches || filteredWorktrees.length > 0;
        const combined = [group.base, ...group.worktrees];
        const lastActivityMs = combined.reduce((latest, repo) => {
            return Math.max(latest, repoLastActivityMs(repo));
        }, 0);
        const flowProgress = combined.reduce((best, repo) => {
            return Math.max(best, repoFlowProgress(repo));
        }, 0);
        return {
            base: group.base,
            worktrees: [...group.worktrees],
            filteredWorktrees,
            matchesFilter,
            pinned: pinnedParentRepoIds.has(group.base.id),
            lastActivityMs,
            flowProgress,
        };
    });
    return { groups, orphanWorktrees, chatBoundWorktrees };
}
function renderRepos(repos) {
    if (!repoListEl)
        return;
    repoListEl.innerHTML = "";
    updateCleanupAllThreadsButton(repos);
    const searchQuery = normalizedHubSearch();
    const repoChannels = channelsByRepoId(hubChannelEntries);
    if (!repos.length) {
        repoListEl.innerHTML = '<div class="hub-empty muted">No repos found.</div>';
        return;
    }
    const { groups, orphanWorktrees, chatBoundWorktrees } = buildRepoGroups(repos);
    const orderedGroups = groups
        .filter((group) => group.matchesFilter)
        .sort((a, b) => {
        if (a.pinned !== b.pinned)
            return a.pinned ? -1 : 1;
        if (hubViewPrefs.sortOrder === "last_activity_desc") {
            return (b.lastActivityMs - a.lastActivityMs ||
                String(a.base.id).localeCompare(String(b.base.id)));
        }
        if (hubViewPrefs.sortOrder === "last_activity_asc") {
            return (a.lastActivityMs - b.lastActivityMs ||
                String(a.base.id).localeCompare(String(b.base.id)));
        }
        if (hubViewPrefs.sortOrder === "flow_progress_desc") {
            return (b.flowProgress - a.flowProgress ||
                b.lastActivityMs - a.lastActivityMs ||
                String(a.base.id).localeCompare(String(b.base.id)));
        }
        return String(a.base.id).localeCompare(String(b.base.id));
    });
    const filteredOrphans = hubViewPrefs.flowFilter === "all"
        ? [...orphanWorktrees]
        : orphanWorktrees.filter((repo) => repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter));
    const queryFilteredOrphans = filteredOrphans
        .map((repo) => {
        const channels = repoChannels.get(repo.id) || [];
        if (!searchQuery) {
            return { repo, channels };
        }
        const repoMatch = repoMatchesSearch(repo, searchQuery);
        const channelMatches = channels.filter((channel) => channelMatchesSearch(channel, searchQuery));
        if (!repoMatch && !channelMatches.length) {
            return null;
        }
        return {
            repo,
            channels: repoMatch ? channels : channelMatches,
        };
    })
        .filter((item) => Boolean(item));
    filteredOrphans.sort((a, b) => compareReposForSort(a, b, hubViewPrefs.sortOrder));
    const filteredChatBound = hubViewPrefs.flowFilter === "all"
        ? [...chatBoundWorktrees]
        : chatBoundWorktrees.filter((repo) => repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter));
    const queryFilteredChatBound = filteredChatBound
        .map((repo) => {
        const channels = repoChannels.get(repo.id) || [];
        if (!searchQuery) {
            return { repo, channels };
        }
        const repoMatch = repoMatchesSearch(repo, searchQuery);
        const channelMatches = channels.filter((channel) => channelMatchesSearch(channel, searchQuery));
        if (!repoMatch && !channelMatches.length) {
            return null;
        }
        return {
            repo,
            channels: repoMatch ? channels : channelMatches,
        };
    })
        .filter((item) => Boolean(item));
    queryFilteredOrphans.sort((a, b) => compareReposForSort(a.repo, b.repo, hubViewPrefs.sortOrder));
    queryFilteredChatBound.sort((a, b) => compareReposForSort(a.repo, b.repo, hubViewPrefs.sortOrder));
    if (!orderedGroups.length &&
        !queryFilteredOrphans.length &&
        !queryFilteredChatBound.length) {
        repoListEl.innerHTML =
            '<div class="hub-empty muted">No rows match current filters.</div>';
        return;
    }
    const renderRepoCard = (repo, { isWorktreeRow = false, inlineChannels = [], } = {}) => {
        const card = document.createElement("div");
        card.className = isWorktreeRow
            ? "hub-repo-card hub-worktree-card"
            : "hub-repo-card";
        card.dataset.repoId = repo.id;
        const canNavigate = repo.mounted === true;
        if (canNavigate) {
            card.classList.add("hub-repo-clickable");
            card.dataset.href = resolvePath(`/repos/${repo.id}/`);
            card.setAttribute("role", "link");
            card.setAttribute("tabindex", "0");
        }
        const actions = buildActions(repo)
            .map((action) => `<button class="${action.kind} sm" data-action="${escapeHtml(action.key)}" data-repo="${escapeHtml(repo.id)}"${action.title ? ` title="${escapeHtml(action.title)}"` : ""}${action.disabled ? " disabled" : ""}>${escapeHtml(action.label)}</button>`)
            .join("");
        const isPinnedParent = !isWorktreeRow && repo.kind === "base" && pinnedParentRepoIds.has(repo.id);
        const pinAction = !isWorktreeRow && repo.kind === "base"
            ? `<button class="ghost sm icon-btn hub-pin-btn${isPinnedParent ? " active" : ""}" data-action="${isPinnedParent ? "unpin_parent" : "pin_parent"}" data-repo="${escapeHtml(repo.id)}" title="${isPinnedParent ? "Unpin parent repo" : "Pin parent repo"}" aria-label="${isPinnedParent ? "Unpin parent repo" : "Pin parent repo"}"><span class="hub-pin-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6"></path><path d="M12 14v7"></path></svg></span></button>`
            : "";
        const flowDisplay = repo.ticket_flow_display;
        const statusText = flowDisplay?.status_label || repo.status;
        const statusValue = flowDisplay?.status || repo.status;
        const statusBadge = buildFlowStatusBadge(statusText, statusValue);
        const mountBadge = buildMountBadge(repo);
        const destinationBadge = buildDestinationBadge(repo.effective_destination);
        const freshness = repoFreshness(repo);
        const freshnessBadge = freshness?.is_stale === true
            ? `<span class="pill pill-small pill-warn" title="${escapeHtml(freshnessSummary(freshness) || "Snapshot data is stale")}">stale</span>`
            : "";
        const lockBadge = repo.lock_status && repo.lock_status !== "unlocked"
            ? `<span class="pill pill-small pill-warn">${escapeHtml(repo.lock_status.replace("_", " "))}</span>`
            : "";
        const initBadge = !repo.initialized
            ? '<span class="pill pill-small pill-warn">uninit</span>'
            : "";
        let noteText = "";
        if (!repo.exists_on_disk) {
            noteText = "Missing on disk";
        }
        else if (repo.init_error) {
            noteText = repo.init_error;
        }
        else if (repo.mount_error) {
            noteText = `Cannot open: ${repo.mount_error}`;
        }
        const note = noteText
            ? `<div class="hub-repo-note">${escapeHtml(noteText)}</div>`
            : "";
        const openIndicator = canNavigate
            ? '<span class="hub-repo-open-indicator">→</span>'
            : "";
        const runSummary = formatRunSummary(repo);
        const lastActivity = formatLastActivity(repo);
        const runDuration = repo.last_run_finished_at ? formatRunDuration(repo.last_run_duration_seconds) : "";
        const pmaChannels = inlineChannels.filter((channel) => isManagedPmaChannel(channel));
        const visibleChannels = inlineChannels.filter((channel) => !isManagedPmaChannel(channel));
        const pmaGroups = groupPmaChannels(pmaChannels.filter((channel) => !isDuplicateChatBoundPmaChannel(channel, visibleChannels)));
        const primaryChannel = visibleChannels[0] || null;
        const infoItems = [];
        if (!primaryChannel) {
            if (runSummary &&
                runSummary !== "No runs yet" &&
                runSummary !== "Not initialized") {
                infoItems.push(runSummary);
            }
            if (lastActivity) {
                infoItems.push(lastActivity);
            }
            if (runDuration) {
                infoItems.push(`took ${runDuration}`);
            }
        }
        if (freshness?.is_stale === true) {
            const staleSummary = freshnessSummary(freshness);
            infoItems.push(staleSummary ? `Snapshot stale · ${staleSummary}` : "Snapshot stale");
        }
        const infoSubline = primaryChannel
            ? channelSummarySubline(primaryChannel, {
                lastActivity,
                additionalCount: Math.max(0, visibleChannels.length - 1),
            })
            : pmaGroups.length > 0
                ? pmaSummaryMarkup(pmaGroups[0].latest, {
                    label: pmaGroups[0].label,
                    count: pmaGroups[0].count,
                    lastActivity,
                })
                : infoItems.length > 0
                    ? `<div class="hub-repo-subline"><span class="hub-repo-info-line">${escapeHtml(infoItems.join(" · "))}</span></div>`
                    : "";
        const overflowChannelRows = visibleChannels
            .slice(1)
            .map((channel) => {
            const label = channelDisplayLabel(channel);
            const sourceBadge = channelSourceBadgeMarkup(channel);
            return `
          <div class="hub-chat-binding-row">
            <div class="hub-chat-binding-main">
              ${sourceBadge}
              <span class="hub-chat-binding-label">${escapeHtml(label)}</span>
            </div>
            <div class="hub-chat-binding-meta muted small">${escapeHtml(channelMetaSummary(channel, { includeRepo: false }))}</div>
          </div>
        `;
        })
            .join("");
        const pmaRows = pmaGroups
            .map((group, index) => {
            if (!primaryChannel && index === 0)
                return "";
            return pmaSummaryMarkup(group.latest, {
                label: group.label,
                count: group.count,
            });
        })
            .join("");
        const pmaBlock = primaryChannel && pmaGroups.length > 0
            ? pmaRows
            : "";
        const inlineChannelBlock = overflowChannelRows || pmaRows
            ? `<div class="hub-chat-binding-block">${pmaBlock || ""}${overflowChannelRows}${!primaryChannel ? pmaRows : ""}</div>`
            : "";
        const setupBadge = (repo.worktree_setup_commands || []).length > 0 && repo.kind === "base"
            ? '<span class="pill pill-small pill-success">setup</span>'
            : "";
        const metadataBadges = [
            destinationBadge,
            statusBadge,
            freshnessBadge,
            mountBadge,
            lockBadge,
            initBadge,
            setupBadge,
        ]
            .filter(Boolean)
            .join("");
        const usageInfo = getRepoUsage(repo.id);
        const usageBadge = `<span class="pill pill-small hub-usage-pill${usageInfo.hasData ? "" : " muted"}">${escapeHtml(usageInfo.label)}</span>`;
        // Ticket flow progress line
        let ticketFlowLine = "";
        const tf = repo.ticket_flow;
        if (flowDisplay && flowDisplay.total_count > 0) {
            const percent = Math.round((flowDisplay.done_count / flowDisplay.total_count) * 100);
            const isActive = Boolean(flowDisplay.is_active);
            const currentStep = tf?.current_step;
            const statusSuffix = flowDisplay.status === "paused"
                ? " · paused"
                : currentStep
                    ? ` · step ${currentStep}`
                    : "";
            ticketFlowLine = `
        <div class="hub-repo-flow-line${isActive ? " active" : ""}">
          <div class="hub-flow-bar">
            <div class="hub-flow-fill" style="width:${percent}%"></div>
          </div>
          <span class="hub-flow-text">${escapeHtml(flowDisplay.status_label)} ${flowDisplay.done_count}/${flowDisplay.total_count}${statusSuffix}</span>
        </div>`;
        }
        card.innerHTML = `
      <div class="hub-repo-row">
        ${pinAction ? `<div class="hub-repo-left">${pinAction}</div>` : ""}
        <div class="hub-repo-center">
          <div class="hub-repo-mainline">
            <span class="hub-repo-title">${escapeHtml(repo.display_name)}</span>
            <div class="hub-repo-meta-inline">${metadataBadges}</div>
            ${usageBadge}
          </div>
          ${infoSubline}
          ${ticketFlowLine}
          ${inlineChannelBlock}
        </div>
        <div class="hub-repo-right">
          ${actions || ""}
          ${openIndicator}
        </div>
      </div>
      ${note}
    `;
        const statusEl = card.querySelector(".hub-status-pill");
        if (statusEl) {
            statusPill(statusEl, flowDisplay?.status || repo.status);
        }
        repoListEl.appendChild(card);
    };
    let renderedRepoRows = 0;
    orderedGroups.forEach((group) => {
        const baseChannels = repoChannels.get(group.base.id) || [];
        const baseRepoMatchesQuery = repoMatchesSearch(group.base, searchQuery);
        const matchedBaseChannels = searchQuery
            ? baseChannels.filter((channel) => channelMatchesSearch(channel, searchQuery))
            : baseChannels;
        const baseMatchesQuery = !searchQuery || baseRepoMatchesQuery || matchedBaseChannels.length > 0;
        const worktrees = [...group.filteredWorktrees]
            .map((repo) => {
            const channels = repoChannels.get(repo.id) || [];
            if (!searchQuery) {
                return { repo, channels };
            }
            const repoMatch = repoMatchesSearch(repo, searchQuery);
            const channelMatches = channels.filter((channel) => channelMatchesSearch(channel, searchQuery));
            if (!repoMatch && !channelMatches.length) {
                return null;
            }
            return {
                repo,
                channels: repoMatch ? channels : channelMatches,
            };
        })
            .filter((item) => Boolean(item))
            .sort((a, b) => compareReposForSort(a.repo, b.repo, hubViewPrefs.sortOrder));
        const hasRepoMatch = !searchQuery || baseMatchesQuery || worktrees.length > 0;
        if (!hasRepoMatch)
            return;
        const repo = group.base;
        renderRepoCard(repo, {
            isWorktreeRow: false,
            inlineChannels: baseRepoMatchesQuery ? baseChannels : matchedBaseChannels,
        });
        renderedRepoRows += 1;
        if (worktrees.length) {
            const list = document.createElement("div");
            list.className = "hub-worktree-list";
            worktrees.forEach(({ repo: wt, channels }) => {
                const row = document.createElement("div");
                row.className = "hub-worktree-row";
                const tmp = document.createElement("div");
                tmp.className = "hub-worktree-row-inner";
                list.appendChild(tmp);
                const beforeCount = repoListEl.children.length;
                renderRepoCard(wt, { isWorktreeRow: true, inlineChannels: channels });
                const newNode = repoListEl.children[beforeCount];
                if (newNode) {
                    repoListEl.removeChild(newNode);
                    tmp.appendChild(newNode);
                    renderedRepoRows += 1;
                }
            });
            repoListEl.appendChild(list);
        }
    });
    if (queryFilteredOrphans.length) {
        const header = document.createElement("div");
        header.className = "hub-worktree-orphans muted small";
        header.textContent = "Orphan worktrees";
        repoListEl.appendChild(header);
        queryFilteredOrphans.forEach(({ repo, channels }) => {
            renderRepoCard(repo, { isWorktreeRow: true, inlineChannels: channels });
            renderedRepoRows += 1;
        });
    }
    if (queryFilteredChatBound.length) {
        const header = document.createElement("div");
        header.className = "hub-worktree-orphans muted small";
        header.textContent = "Chat bound worktrees";
        repoListEl.appendChild(header);
        queryFilteredChatBound.forEach(({ repo, channels }) => {
            renderRepoCard(repo, {
                isWorktreeRow: true,
                inlineChannels: channels,
            });
            renderedRepoRows += 1;
        });
    }
    if (!renderedRepoRows) {
        repoListEl.innerHTML =
            '<div class="hub-empty muted">No rows match current filters.</div>';
        return;
    }
    if (hubUsageUnmatched && hubUsageUnmatched.events) {
        const note = document.createElement("div");
        note.className = "hub-usage-unmatched-note muted small";
        const total = formatTokensCompact(hubUsageUnmatched.totals?.total_tokens);
        note.textContent = `Other: ${total} · ${hubUsageUnmatched.events}ev (unattributed)`;
        repoListEl.appendChild(note);
    }
}
function renderReposWithScroll(repos) {
    preserveScroll(repoListEl, () => {
        renderRepos(repos);
    }, { restoreOnNextFrame: true });
}
function renderAgentWorkspaces(agentWorkspaces) {
    if (!agentWorkspaceListEl)
        return;
    agentWorkspaceListEl.innerHTML = "";
    if (!agentWorkspaces.length) {
        agentWorkspaceListEl.innerHTML =
            '<div class="hub-empty muted">No agent workspaces yet.</div>';
        return;
    }
    const ordered = [...agentWorkspaces].sort((a, b) => {
        const aLabel = String(a.display_name || a.id);
        const bLabel = String(b.display_name || b.id);
        return aLabel.localeCompare(bLabel) || String(a.id).localeCompare(String(b.id));
    });
    const workspaceChannels = channelsByAgentWorkspaceId(hubChannelEntries);
    ordered.forEach((workspace) => {
        const card = document.createElement("div");
        card.className = "hub-repo-card";
        card.dataset.agentWorkspaceId = workspace.id;
        const actions = buildAgentWorkspaceActions(workspace)
            .map((action) => `<button class="${action.kind} sm" data-agent-workspace="${escapeHtml(workspace.id)}" data-action="${escapeHtml(action.key)}"${action.title ? ` title="${escapeHtml(action.title)}"` : ""}>${escapeHtml(action.label)}</button>`)
            .join("");
        const enabledBadge = workspace.enabled
            ? '<span class="pill pill-small pill-success">enabled</span>'
            : '<span class="pill pill-small pill-warn">disabled</span>';
        const runtimeBadge = `<span class="pill pill-small pill-idle">${escapeHtml(workspace.runtime)}</span>`;
        const destinationBadge = buildDestinationBadge(workspace.effective_destination);
        const missingBadge = !workspace.exists_on_disk
            ? '<span class="pill pill-small pill-warn">missing</span>'
            : "";
        const destinationSummary = formatDestinationSummary(workspace.effective_destination);
        const infoSummary = [
            `runtime ${workspace.runtime}`,
            `destination ${destinationSummary}`,
        ].join(" · ");
        const pathSummary = escapeHtml(workspace.path);
        const inlineChannels = workspaceChannels.get(workspace.id) || [];
        const primaryChannel = inlineChannels[0] || null;
        const infoSubline = primaryChannel
            ? channelSummarySubline(primaryChannel, {
                additionalCount: Math.max(0, inlineChannels.length - 1),
            })
            : `<div class="hub-repo-subline">
          <span class="hub-repo-info-line">${escapeHtml(infoSummary)}</span>
        </div>`;
        const overflowChannelRows = inlineChannels
            .slice(1)
            .map((channel) => {
            const label = channelDisplayLabel(channel);
            const sourceBadge = channelSourceBadgeMarkup(channel);
            return `
          <div class="hub-chat-binding-row">
            <div class="hub-chat-binding-main">
              ${sourceBadge}
              <span class="hub-chat-binding-label">${escapeHtml(label)}</span>
            </div>
            <div class="hub-chat-binding-meta muted small">${escapeHtml(channelMetaSummary(channel, { includeRepo: false }))}</div>
          </div>
        `;
        })
            .join("");
        const inlineChannelBlock = overflowChannelRows
            ? `<div class="hub-chat-binding-block">${overflowChannelRows}</div>`
            : "";
        card.innerHTML = `
      <div class="hub-repo-row">
        <div class="hub-repo-center">
          <div class="hub-repo-mainline">
            <span class="hub-repo-title">${escapeHtml(workspace.display_name || workspace.id)}</span>
            <div class="hub-repo-meta-inline">
              ${runtimeBadge}
              ${enabledBadge}
              ${destinationBadge}
              ${missingBadge}
            </div>
          </div>
          ${infoSubline}
          <div class="hub-repo-subline">
            <span class="hub-chat-binding-key">${pathSummary}</span>
          </div>
          ${inlineChannelBlock}
        </div>
        <div class="hub-repo-right">
          ${actions}
        </div>
      </div>
    `;
        agentWorkspaceListEl.appendChild(card);
    });
}
function applyHubData(data) {
    hubData = {
        repos: Array.isArray(data?.repos) ? data.repos : [],
        agent_workspaces: Array.isArray(data?.agent_workspaces)
            ? data.agent_workspaces
            : [],
        last_scan_at: data?.last_scan_at || null,
        pinned_parent_repo_ids: normalizePinnedParentRepoIds(data?.pinned_parent_repo_ids),
    };
    pinnedParentRepoIds = new Set(normalizePinnedParentRepoIds(hubData.pinned_parent_repo_ids));
}
async function refreshHub() {
    setButtonLoading(true);
    try {
        const data = await api("/hub/repos", { method: "GET" });
        applyHubData(data);
        markHubRefreshed();
        saveSessionCache(HUB_CACHE_KEY, hubData);
        renderSummary(hubData.repos || []);
        renderReposWithScroll(hubData.repos || []);
        renderAgentWorkspaces(hubData.agent_workspaces || []);
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
async function triggerHubScan() {
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
async function createRepo(repoId, repoPath, gitInit, gitUrl) {
    try {
        const payload = {};
        if (repoId)
            payload.id = repoId;
        if (repoPath)
            payload.path = repoPath;
        payload.git_init = gitInit;
        if (gitUrl)
            payload.git_url = gitUrl;
        const job = await startHubJob("/hub/jobs/repos", {
            body: payload,
            startedMessage: "Repo creation queued",
        });
        const label = repoId || repoPath || "repo";
        flash(`Created repo: ${label}`, "success");
        await refreshHub();
        if (job?.result?.mounted && job?.result?.id) {
            window.location.href = resolvePath(`/repos/${job.result.id}/`);
        }
        return true;
    }
    catch (err) {
        flash(err.message || "Failed to create repo", "error");
        return false;
    }
}
async function createAgentWorkspace(workspaceId, runtime, displayName) {
    try {
        const payload = {};
        if (workspaceId)
            payload.id = workspaceId;
        if (runtime)
            payload.runtime = runtime;
        if (displayName)
            payload.display_name = displayName;
        await startHubJob("/hub/jobs/agent-workspaces", {
            body: payload,
            startedMessage: "Agent workspace creation queued",
        });
        flash(`Created agent workspace: ${workspaceId || displayName || "workspace"}`, "success");
        await refreshHub();
        return true;
    }
    catch (err) {
        flash(err.message || "Failed to create agent workspace", "error");
        return false;
    }
}
let closeCreateRepoModal = null;
let closeCreateAgentWorkspaceModal = null;
function hideCreateRepoModal() {
    if (closeCreateRepoModal) {
        const close = closeCreateRepoModal;
        closeCreateRepoModal = null;
        close();
    }
}
function hideCreateAgentWorkspaceModal() {
    if (closeCreateAgentWorkspaceModal) {
        const close = closeCreateAgentWorkspaceModal;
        closeCreateAgentWorkspaceModal = null;
        close();
    }
}
function showCreateRepoModal() {
    const modal = document.getElementById("create-repo-modal");
    if (!modal)
        return;
    const triggerEl = document.activeElement;
    hideCreateRepoModal();
    const input = document.getElementById("create-repo-id");
    closeCreateRepoModal = openModal(modal, {
        initialFocus: input || modal,
        returnFocusTo: triggerEl,
        onRequestClose: hideCreateRepoModal,
    });
    if (input) {
        input.value = "";
        input.focus();
    }
    const pathInput = document.getElementById("create-repo-path");
    if (pathInput)
        pathInput.value = "";
    const urlInput = document.getElementById("create-repo-url");
    if (urlInput)
        urlInput.value = "";
    const gitCheck = document.getElementById("create-repo-git");
    if (gitCheck)
        gitCheck.checked = true;
}
function showCreateAgentWorkspaceModal() {
    const modal = document.getElementById("create-agent-workspace-modal");
    if (!modal)
        return;
    const triggerEl = document.activeElement;
    hideCreateAgentWorkspaceModal();
    const input = document.getElementById("create-agent-workspace-id");
    closeCreateAgentWorkspaceModal = openModal(modal, {
        initialFocus: input || modal,
        returnFocusTo: triggerEl,
        onRequestClose: hideCreateAgentWorkspaceModal,
    });
    if (input) {
        input.value = "";
        input.focus();
    }
    const runtimeInput = document.getElementById("create-agent-workspace-runtime");
    if (runtimeInput)
        runtimeInput.value = "";
    const nameInput = document.getElementById("create-agent-workspace-name");
    if (nameInput)
        nameInput.value = "";
}
async function handleCreateRepoSubmit() {
    const idInput = document.getElementById("create-repo-id");
    const pathInput = document.getElementById("create-repo-path");
    const urlInput = document.getElementById("create-repo-url");
    const gitCheck = document.getElementById("create-repo-git");
    const repoId = idInput?.value?.trim() || null;
    const repoPath = pathInput?.value?.trim() || null;
    const gitUrl = urlInput?.value?.trim() || null;
    const gitInit = gitCheck?.checked ?? true;
    if (!repoId && !gitUrl) {
        flash("Repo ID or Git URL is required", "error");
        return;
    }
    const ok = await createRepo(repoId, repoPath, gitInit, gitUrl);
    if (ok) {
        hideCreateRepoModal();
    }
}
async function handleCreateAgentWorkspaceSubmit() {
    const idInput = document.getElementById("create-agent-workspace-id");
    const runtimeInput = document.getElementById("create-agent-workspace-runtime");
    const nameInput = document.getElementById("create-agent-workspace-name");
    const workspaceId = idInput?.value?.trim() || null;
    const runtime = runtimeInput?.value?.trim() || null;
    const displayName = nameInput?.value?.trim() || null;
    if (!workspaceId || !runtime) {
        flash("Workspace ID and runtime are required", "error");
        return;
    }
    const ok = await createAgentWorkspace(workspaceId, runtime, displayName);
    if (ok) {
        hideCreateAgentWorkspaceModal();
    }
}
function initHubRepoListControls() {
    loadHubViewPrefs();
    if (hubFlowFilterEl) {
        hubFlowFilterEl.value = hubViewPrefs.flowFilter;
        hubFlowFilterEl.addEventListener("change", () => {
            hubViewPrefs.flowFilter = hubFlowFilterEl.value;
            saveHubViewPrefs();
            renderReposWithScroll(hubData.repos || []);
        });
    }
    if (hubSortOrderEl) {
        hubSortOrderEl.value = hubViewPrefs.sortOrder;
        hubSortOrderEl.addEventListener("change", () => {
            hubViewPrefs.sortOrder = hubSortOrderEl.value;
            saveHubViewPrefs();
            renderReposWithScroll(hubData.repos || []);
        });
    }
}
function applyHubPanelState(openPanel) {
    hubOpenPanel = openPanel;
    const reposOpen = openPanel === "repos";
    const agentsOpen = openPanel === "agents";
    hubShellEl?.setAttribute("data-hub-open-panel", openPanel);
    hubRepoPanelEl?.classList.toggle("hub-panel-expanded", reposOpen);
    hubRepoPanelEl?.classList.toggle("hub-panel-collapsed", !reposOpen);
    hubAgentPanelEl?.classList.toggle("hub-panel-expanded", agentsOpen);
    hubAgentPanelEl?.classList.toggle("hub-panel-collapsed", !agentsOpen);
    if (hubRepoPanelSummaryEl) {
        hubRepoPanelSummaryEl.setAttribute("aria-expanded", reposOpen ? "true" : "false");
    }
    if (hubRepoPanelStateEl) {
        hubRepoPanelStateEl.textContent = reposOpen ? "Expanded" : "Show panel";
    }
    if (hubAgentPanelSummaryEl) {
        hubAgentPanelSummaryEl.setAttribute("aria-expanded", agentsOpen ? "true" : "false");
    }
    if (hubAgentPanelStateEl) {
        hubAgentPanelStateEl.textContent = agentsOpen ? "Expanded" : "Show panel";
    }
}
function toggleHubPanel(panel) {
    if (hubOpenPanel === panel)
        return;
    saveHubOpenPanel(panel);
    applyHubPanelState(panel);
}
function initHubPanelControls() {
    applyHubPanelState(hubOpenPanel);
    hubRepoPanelSummaryEl?.addEventListener("click", () => {
        toggleHubPanel("repos");
    });
    hubAgentPanelSummaryEl?.addEventListener("click", () => {
        toggleHubPanel("agents");
    });
}
async function setParentRepoPinned(repoId, pinned) {
    const response = await api(`/hub/repos/${encodeURIComponent(repoId)}/pin`, {
        method: "POST",
        body: { pinned },
    });
    pinnedParentRepoIds = new Set(normalizePinnedParentRepoIds(response?.pinned_parent_repo_ids));
    hubData.pinned_parent_repo_ids = Array.from(pinnedParentRepoIds);
}
async function chooseDestinationKind(resourceLabel, currentKind) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog repo-settings-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const header = document.createElement("div");
    header.className = "modal-header";
    const title = document.createElement("span");
    title.className = "label";
    title.textContent = `Set destination: ${resourceLabel}`;
    header.appendChild(title);
    const body = document.createElement("div");
    body.className = "modal-body";
    const hint = document.createElement("p");
    hint.className = "muted small";
    hint.textContent = "Choose execution destination kind.";
    body.appendChild(hint);
    const footer = document.createElement("div");
    footer.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "ghost";
    cancelBtn.textContent = "Cancel";
    const localBtn = document.createElement("button");
    localBtn.className = currentKind === "local" ? "primary" : "ghost";
    localBtn.textContent = "Local";
    const dockerBtn = document.createElement("button");
    dockerBtn.className = currentKind === "docker" ? "primary" : "ghost";
    dockerBtn.textContent = "Docker";
    footer.append(cancelBtn, localBtn, dockerBtn);
    dialog.append(header, body, footer);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    return new Promise((resolve) => {
        let closeModal = null;
        let settled = false;
        const returnFocusTo = document.activeElement;
        const finalize = (selected) => {
            if (settled)
                return;
            settled = true;
            if (closeModal) {
                const close = closeModal;
                closeModal = null;
                close();
            }
            overlay.remove();
            resolve(selected);
        };
        closeModal = openModal(overlay, {
            initialFocus: currentKind === "docker" ? dockerBtn : localBtn,
            returnFocusTo,
            onRequestClose: () => finalize(null),
        });
        cancelBtn.addEventListener("click", () => finalize(null));
        localBtn.addEventListener("click", () => finalize("local"));
        dockerBtn.addEventListener("click", () => finalize("docker"));
    });
}
async function promptForDestinationBody(resourceLabel, currentDestination) {
    const current = formatDestinationSummary(currentDestination);
    const currentKind = current.startsWith("docker:") || current === "docker" ? "docker" : "local";
    const kind = await chooseDestinationKind(resourceLabel, currentKind);
    if (!kind)
        return null;
    const body = { kind };
    if (kind === "docker") {
        const currentImage = typeof currentDestination?.image === "string"
            ? String(currentDestination.image)
            : "";
        const imageValue = await inputModal("Docker image:", {
            placeholder: "ghcr.io/acme/repo:tag",
            defaultValue: currentImage,
            confirmText: "Save",
        });
        if (!imageValue) {
            flash("Docker destination requires an image", "error");
            return null;
        }
        body.image = imageValue.trim();
        const configureAdvanced = await confirmModal("Configure optional docker fields (container name, profile, workdir, env passthrough, explicit env, mounts)?", {
            confirmText: "Configure",
            cancelText: "Skip",
            danger: false,
        });
        if (configureAdvanced) {
            const currentContainerName = typeof currentDestination?.container_name === "string"
                ? String(currentDestination.container_name)
                : "";
            const containerNameValue = await inputModal("Docker container name (optional):", {
                placeholder: "car-runner",
                defaultValue: currentContainerName,
                confirmText: "Next",
                allowEmpty: true,
            });
            if (containerNameValue === null)
                return null;
            const containerName = containerNameValue.trim();
            if (containerName) {
                body.container_name = containerName;
            }
            const profileValue = await inputModal("Docker profile (optional):", {
                placeholder: "full-dev",
                defaultValue: currentDockerProfile(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (profileValue === null)
                return null;
            const profile = profileValue.trim();
            if (profile) {
                body.profile = profile;
            }
            const workdirValue = await inputModal("Docker workdir (optional):", {
                placeholder: "/workspace",
                defaultValue: currentDockerWorkdir(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (workdirValue === null)
                return null;
            const workdir = workdirValue.trim();
            if (workdir) {
                body.workdir = workdir;
            }
            const envPassthroughValue = await inputModal("Docker env passthrough (optional, comma-separated):", {
                placeholder: "CAR_*, PATH",
                defaultValue: currentDockerEnvPassthrough(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (envPassthroughValue === null)
                return null;
            const envPassthrough = splitCommaSeparated(envPassthroughValue);
            if (envPassthrough.length) {
                body.env_passthrough = envPassthrough;
            }
            const envMapValue = await inputModal("Docker explicit env map (optional, KEY=VALUE pairs, comma-separated):", {
                placeholder: "OPENAI_API_KEY=sk-..., CODEX_HOME=/workspace/.codex",
                defaultValue: currentDockerExplicitEnv(currentDestination),
                confirmText: "Next",
                allowEmpty: true,
            });
            if (envMapValue === null)
                return null;
            const parsedEnvMap = parseDockerEnvMap(envMapValue);
            if (parsedEnvMap.error) {
                flash(parsedEnvMap.error, "error");
                return null;
            }
            if (Object.keys(parsedEnvMap.env).length) {
                body.env = parsedEnvMap.env;
            }
            const mountsValue = await inputModal("Docker mounts (optional, source:target[:ro] pairs, comma-separated):", {
                placeholder: "/host/path:/workspace/path, /cache:/cache:ro",
                defaultValue: currentDockerMounts(currentDestination),
                confirmText: "Save",
                allowEmpty: true,
            });
            if (mountsValue === null)
                return null;
            const parsedMounts = parseDockerMountList(mountsValue);
            if (parsedMounts.error) {
                flash(parsedMounts.error, "error");
                return null;
            }
            if (parsedMounts.mounts.length) {
                body.mounts = parsedMounts.mounts;
            }
        }
    }
    return body;
}
async function promptAndSetRepoDestination(repo) {
    const body = await promptForDestinationBody(repo.display_name || repo.id, repo.effective_destination);
    if (!body)
        return false;
    const payload = (await api(`/hub/repos/${encodeURIComponent(repo.id)}/destination`, {
        method: "POST",
        body,
    }));
    const effective = formatDestinationSummary(payload.effective_destination);
    flash(`Updated destination for ${repo.id}: ${effective}`, "success");
    return true;
}
async function promptAndSetAgentWorkspaceDestination(workspace) {
    const body = await promptForDestinationBody(workspace.display_name || workspace.id, workspace.effective_destination);
    if (!body)
        return false;
    const payload = (await api(`/hub/agent-workspaces/${encodeURIComponent(workspace.id)}/destination`, {
        method: "POST",
        body,
    }));
    const effective = formatDestinationSummary(payload.effective_destination);
    flash(`Updated destination for ${workspace.id}: ${effective}`, "success");
    return true;
}
async function loadHubChannelDirectory({ silent = false } = {}) {
    try {
        const payload = (await api("/hub/chat/channels?limit=1000", {
            method: "GET",
        }));
        hubChannelEntries = Array.isArray(payload.entries) ? payload.entries : [];
        renderReposWithScroll(hubData.repos || []);
    }
    catch (err) {
        if (!silent) {
            flash(err.message || "Failed to load channel directory", "error");
        }
    }
}
async function removeRepoWithChecks(repoId) {
    const check = await api(`/hub/repos/${repoId}/remove-check`, {
        method: "GET",
    });
    const warnings = [];
    const dirty = check.is_clean === false;
    if (dirty) {
        warnings.push("Working tree has uncommitted changes.");
    }
    const upstream = check.upstream;
    const hasUpstream = upstream?.has_upstream === false;
    if (hasUpstream) {
        warnings.push("No upstream tracking branch is configured.");
    }
    const ahead = Number(upstream?.ahead || 0);
    if (ahead > 0) {
        warnings.push(`Local branch is ahead of upstream by ${ahead} commit(s).`);
    }
    const behind = Number(upstream?.behind || 0);
    if (behind > 0) {
        warnings.push(`Local branch is behind upstream by ${behind} commit(s).`);
    }
    const worktrees = Array.isArray(check.worktrees)
        ? check.worktrees
        : [];
    if (worktrees.length) {
        warnings.push(`This repo has ${worktrees.length} worktree(s).`);
    }
    const messageParts = [`Remove repo "${repoId}" and delete its local directory?`];
    if (warnings.length) {
        messageParts.push("", "Warnings:", ...warnings.map((w) => `- ${w}`));
    }
    if (worktrees.length) {
        messageParts.push("", "Worktrees to delete:", ...worktrees.map((w) => `- ${w}`));
    }
    const ok = await confirmModal(messageParts.join("\n"), {
        confirmText: "Remove",
        danger: true,
    });
    if (!ok)
        return;
    const needsForce = dirty || ahead > 0;
    const requestBody = {
        force: needsForce,
        delete_dir: true,
        delete_worktrees: worktrees.length > 0,
    };
    if (needsForce) {
        const requiredAttestation = `REMOVE ${repoId}`;
        const forceAttestation = await inputModal(`This repo has uncommitted or unpushed changes.\n\nType this confirmation text to force removal:\n${requiredAttestation}`, { placeholder: requiredAttestation, confirmText: "Remove anyway" });
        if (!forceAttestation)
            return;
        if (forceAttestation !== requiredAttestation) {
            flash(`Confirmation text must exactly match: ${requiredAttestation}`, "error");
            return;
        }
        requestBody.force_attestation = forceAttestation;
    }
    await startHubJob(`/hub/jobs/repos/${repoId}/remove`, {
        body: requestBody,
        startedMessage: "Repo removal queued",
    });
    flash(`Removed repo: ${repoId}`, "success");
    await refreshHub();
}
async function handleAgentWorkspaceAction(workspaceId, action) {
    const buttons = agentWorkspaceListEl?.querySelectorAll(`button[data-agent-workspace="${workspaceId}"][data-action="${action}"]`);
    buttons?.forEach((btn) => (btn.disabled = true));
    try {
        const workspace = hubData.agent_workspaces.find((item) => item.id === workspaceId);
        if (!workspace) {
            flash(`Agent workspace not found: ${workspaceId}`, "error");
            return;
        }
        if (action === "enable" || action === "disable") {
            const enabled = action === "enable";
            await api(`/hub/agent-workspaces/${encodeURIComponent(workspaceId)}`, {
                method: "PATCH",
                body: { enabled },
            });
            flash(`${enabled ? "Enabled" : "Disabled"}: ${workspaceId}`, "success");
            await refreshHub();
            return;
        }
        if (action === "set_destination") {
            const updated = await promptAndSetAgentWorkspaceDestination(workspace);
            if (updated) {
                await refreshHub();
            }
            return;
        }
        if (action === "remove") {
            const ok = await confirmModal(`Remove agent workspace "${workspace.display_name || workspace.id}" from CAR?\n\nManaged files will stay on disk at:\n${workspace.path}`, { confirmText: "Remove" });
            if (!ok)
                return;
            await startHubJob(`/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/remove`, {
                body: { delete_dir: false },
                startedMessage: "Agent workspace removal queued",
            });
            flash(`Removed agent workspace: ${workspaceId}`, "success");
            await refreshHub();
            return;
        }
        if (action === "delete") {
            const ok = await confirmModal(`Delete agent workspace "${workspace.display_name || workspace.id}"?\n\nCAR will unregister it and delete its managed directory:\n${workspace.path}`, { confirmText: "Delete", danger: true });
            if (!ok)
                return;
            await startHubJob(`/hub/jobs/agent-workspaces/${encodeURIComponent(workspaceId)}/delete`, {
                body: { delete_dir: true },
                startedMessage: "Agent workspace delete queued",
            });
            flash(`Deleted agent workspace: ${workspaceId}`, "success");
            await refreshHub();
        }
    }
    catch (err) {
        flash(err.message || "Agent workspace action failed", "error");
    }
    finally {
        buttons?.forEach((btn) => (btn.disabled = false));
    }
}
async function handleRepoAction(repoId, action) {
    const buttons = repoListEl?.querySelectorAll(`button[data-repo="${repoId}"][data-action="${action}"]`);
    buttons?.forEach((btn) => btn.disabled = true);
    try {
        if (action === "pin_parent" || action === "unpin_parent") {
            const pinned = action === "pin_parent";
            await setParentRepoPinned(repoId, pinned);
            renderReposWithScroll(hubData.repos || []);
            flash(`${pinned ? "Pinned" : "Unpinned"}: ${repoId}`, "success");
            return;
        }
        const pathMap = {
            init: `/hub/repos/${repoId}/init`,
            sync_main: `/hub/repos/${repoId}/sync-main`,
        };
        if (action === "new_worktree") {
            const branch = await inputModal("New worktree branch name:", {
                placeholder: "feature/my-branch",
                confirmText: "Create",
            });
            if (!branch)
                return;
            const job = await startHubJob("/hub/jobs/worktrees/create", {
                body: { base_repo_id: repoId, branch },
                startedMessage: "Worktree creation queued",
            });
            const created = job?.result;
            flash(`Created worktree: ${created?.id || branch}`, "success");
            await refreshHub();
            if (created?.mounted) {
                window.location.href = resolvePath(`/repos/${created.id}/`);
            }
            return;
        }
        if (action === "repo_settings") {
            const repo = hubData.repos.find((item) => item.id === repoId);
            if (!repo) {
                flash(`Repo not found: ${repoId}`, "error");
                return;
            }
            await openRepoSettingsModal(repo);
            return;
        }
        if (action === "set_destination") {
            const repo = hubData.repos.find((item) => item.id === repoId);
            if (!repo) {
                flash(`Repo not found: ${repoId}`, "error");
                return;
            }
            const updated = await promptAndSetRepoDestination(repo);
            if (updated) {
                await refreshHub();
            }
            return;
        }
        if (action === "cleanup_worktree") {
            const repo = hubData.repos.find((item) => item.id === repoId);
            if (repo && isCleanupBlockedByChatBinding(repo)) {
                flash("Unbind Discord/Telegram chats before cleaning up this worktree", "error");
                return;
            }
            const displayName = repoId.includes("--")
                ? repoId.split("--").pop()
                : repoId;
            const ok = await confirmModal(`Clean up worktree "${displayName}"?\n\nCAR will archive a review snapshot for the Archive tab, then remove the worktree directory and branch. The default snapshot keeps tickets, contextspace, runs, flow artifacts, and lightweight metadata.`, { confirmText: "Archive & remove" });
            if (!ok)
                return;
            await startHubJob("/hub/jobs/worktrees/cleanup", {
                body: {
                    worktree_repo_id: repoId,
                    archive: true,
                    force_archive: false,
                    archive_note: null,
                },
                startedMessage: "Worktree cleanup queued",
            });
            flash(`Removed worktree: ${repoId}`, "success");
            await refreshHub();
            return;
        }
        if (action === "archive_state") {
            const repo = hubData.repos.find((item) => item.id === repoId);
            const cleanupCount = repo ? unboundManagedThreadCount(repo) : 0;
            if (!repo || (!repo.has_car_state && cleanupCount <= 0))
                return;
            const displayName = repo.display_name || repoId;
            const subject = repo.kind === "worktree" ? "worktree" : "repo";
            const archiveSummary = repo.has_car_state
                ? "archive reviewable runtime artifacts for later viewing in the Archive tab"
                : "skip the snapshot because CAR state is already clean";
            const threadSummary = cleanupCount > 0
                ? ` It will also archive ${cleanupCount} stale non-chat-bound managed thread${cleanupCount === 1 ? "" : "s"}.`
                : "";
            const ok = await confirmModal(`Archive ${subject} "${displayName}"?\n\nCAR will ${archiveSummary} before resetting local CAR state when needed.${threadSummary} Git state is not touched, and active chat bindings remain available for fresh work.`, { confirmText: "Archive" });
            if (!ok)
                return;
            const response = (await api("/hub/repos/archive-state", {
                method: "POST",
                body: { repo_id: repoId, archive_note: null },
            }));
            const archivedThreadCount = typeof response?.archived_thread_count === "number"
                ? response.archived_thread_count
                : 0;
            const snapshotText = response?.snapshot_id
                ? `snapshot ${response.snapshot_id}`
                : "managed threads only";
            const threadText = archivedThreadCount > 0
                ? ` and ${archivedThreadCount} managed thread${archivedThreadCount === 1 ? "" : "s"}`
                : "";
            flash(`Archived ${subject}: ${displayName} (${snapshotText}${threadText})`, "success");
            await refreshHub();
            return;
        }
        if (action === "remove_repo") {
            await removeRepoWithChecks(repoId);
            return;
        }
        const path = pathMap[action];
        if (!path)
            return;
        await api(path, { method: "POST" });
        flash(`${action} sent to ${repoId}`, "success");
        await refreshHub();
    }
    catch (err) {
        flash(err.message || "Hub action failed", "error");
    }
    finally {
        buttons?.forEach((btn) => btn.disabled = false);
    }
}
async function handleCleanupAllRepoThreads() {
    const btn = getHubCleanupAllThreadsBtn();
    if (!btn) {
        flash("Cleanup control is missing from the page.", "error");
        return;
    }
    const cleanupRepos = baseReposWithUnboundThreads(hubData.repos || []);
    const totalThreads = totalUnboundManagedThreadCount(cleanupRepos);
    if (totalThreads <= 0) {
        flash("No stale non-chat threads across base repos", "success");
        return;
    }
    const dirtyRepos = dirtyBaseReposWithUnboundThreads(hubData.repos || []);
    const dirtyWarning = dirtyRepos.length
        ? `\n\nDirty repos:\n${dirtyRepos
            .map((repo) => `- ${repo.display_name || repo.id}`)
            .join("\n")}\n\nThese repos are dirty, but cleanup only archives unbound managed threads.`
        : "";
    const ok = await confirmModal(`Clean up stale non-chat threads across ${cleanupRepos.length} base repo${cleanupRepos.length === 1 ? "" : "s"}?\n\nCAR will archive ${totalThreads} unbound managed thread${totalThreads === 1 ? "" : "s"}. Discord and Telegram-bound threads will stay active.${dirtyWarning}`, { confirmText: "Cleanup all" });
    if (!ok)
        return;
    cleanupAllRepoThreadsInFlight = true;
    updateCleanupAllThreadsButton(hubData.repos || []);
    try {
        const response = (await api("/hub/repos/cleanup-threads", {
            method: "POST",
        }));
        const message = typeof response?.message === "string" && response.message.trim()
            ? response.message.trim()
            : "Cleaned up stale threads across base repos";
        flash(message, "success");
        await refreshHub();
    }
    catch (err) {
        flash(err.message || "Bulk cleanup failed", "error");
    }
    finally {
        cleanupAllRepoThreadsInFlight = false;
        updateCleanupAllThreadsButton(hubData.repos || []);
    }
}
function attachHubHandlers() {
    initHubSettings();
    const refreshBtn = document.getElementById("hub-refresh");
    const newRepoBtn = document.getElementById("hub-new-repo");
    const newAgentBtn = document.getElementById("hub-new-agent");
    const createCancelBtn = document.getElementById("create-repo-cancel");
    const createSubmitBtn = document.getElementById("create-repo-submit");
    const createRepoId = document.getElementById("create-repo-id");
    const createAgentCancelBtn = document.getElementById("create-agent-workspace-cancel");
    const createAgentSubmitBtn = document.getElementById("create-agent-workspace-submit");
    const createAgentId = document.getElementById("create-agent-workspace-id");
    const createAgentRuntime = document.getElementById("create-agent-workspace-runtime");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", () => triggerHubScan());
    }
    if (hubUsageRefresh) {
        hubUsageRefresh.addEventListener("click", () => loadHubUsage());
    }
    hubRepoPanelEl?.addEventListener("click", (e) => {
        const target = e.target;
        const el = target instanceof Element ? target : target.parentElement;
        if (!el?.closest("#hub-cleanup-all-threads"))
            return;
        void handleCleanupAllRepoThreads();
    });
    if (hubRepoSearchInput) {
        hubRepoSearchInput.addEventListener("input", () => {
            renderReposWithScroll(hubData.repos || []);
        });
    }
    if (newRepoBtn) {
        newRepoBtn.addEventListener("click", () => {
            toggleHubPanel("repos");
            showCreateRepoModal();
        });
    }
    if (newAgentBtn) {
        newAgentBtn.addEventListener("click", () => {
            toggleHubPanel("agents");
            showCreateAgentWorkspaceModal();
        });
    }
    if (createCancelBtn) {
        createCancelBtn.addEventListener("click", hideCreateRepoModal);
    }
    if (createSubmitBtn) {
        createSubmitBtn.addEventListener("click", handleCreateRepoSubmit);
    }
    if (createAgentCancelBtn) {
        createAgentCancelBtn.addEventListener("click", hideCreateAgentWorkspaceModal);
    }
    if (createAgentSubmitBtn) {
        createAgentSubmitBtn.addEventListener("click", handleCreateAgentWorkspaceSubmit);
    }
    if (createRepoId) {
        createRepoId.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleCreateRepoSubmit();
            }
        });
    }
    if (createAgentId) {
        createAgentId.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleCreateAgentWorkspaceSubmit();
            }
        });
    }
    if (createAgentRuntime) {
        createAgentRuntime.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleCreateAgentWorkspaceSubmit();
            }
        });
    }
    if (repoListEl) {
        repoListEl.addEventListener("click", (event) => {
            const target = event.target;
            const btn = target instanceof HTMLElement && target.closest("button[data-action]");
            if (btn) {
                event.stopPropagation();
                const action = btn.dataset.action;
                const repoId = btn.dataset.repo;
                if (action && repoId) {
                    handleRepoAction(repoId, action);
                }
                return;
            }
            const card = target instanceof HTMLElement && target.closest(".hub-repo-clickable");
            if (card && card.dataset.href) {
                window.location.href = card.dataset.href;
            }
        });
        repoListEl.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                const target = event.target;
                if (target instanceof HTMLElement &&
                    target.classList.contains("hub-repo-clickable")) {
                    event.preventDefault();
                    if (target.dataset.href) {
                        window.location.href = target.dataset.href;
                    }
                }
            }
        });
        repoListEl.addEventListener("mouseover", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLElement))
                return;
            const card = target.closest(".hub-repo-clickable");
            if (card && card.dataset.href) {
                prefetchRepo(card.dataset.href);
            }
        });
        repoListEl.addEventListener("pointerdown", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLElement))
                return;
            const card = target.closest(".hub-repo-clickable");
            if (card && card.dataset.href) {
                prefetchRepo(card.dataset.href);
            }
        });
    }
    if (agentWorkspaceListEl) {
        agentWorkspaceListEl.addEventListener("click", (event) => {
            const target = event.target;
            const btn = target instanceof HTMLElement
                ? target.closest("button[data-action]")
                : null;
            if (!btn)
                return;
            event.stopPropagation();
            const action = btn.dataset.action;
            const workspaceId = btn.dataset.agentWorkspace;
            if (action && workspaceId) {
                handleAgentWorkspaceAction(workspaceId, action);
            }
        });
    }
}
async function silentRefreshHub() {
    try {
        const data = await api("/hub/repos", { method: "GET" });
        applyHubData(data);
        markHubRefreshed();
        saveSessionCache(HUB_CACHE_KEY, hubData);
        renderSummary(hubData.repos || []);
        renderReposWithScroll(hubData.repos || []);
        renderAgentWorkspaces(hubData.agent_workspaces || []);
        await loadHubUsage({ silent: true, allowRetry: false });
        await loadHubChannelDirectory({ silent: true });
    }
    catch (err) {
        console.error("Auto-refresh hub failed:", err);
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
    const running = hasActiveRuns(hubData.repos || []);
    const minInterval = running ? HUB_REFRESH_ACTIVE_MS : HUB_REFRESH_IDLE_MS;
    if (now - lastHubAutoRefreshAt < minInterval)
        return;
    await silentRefreshHub();
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
function prefetchRepo(url) {
    if (!url || prefetchedUrls.has(url))
        return;
    prefetchedUrls.add(url);
    fetch(url, { method: "GET", headers: { "x-prefetch": "1" } }).catch(() => { });
}
export function initHub() {
    attachHubHandlers();
    initHubRepoListControls();
    initHubPanelControls();
    if (!repoListEl)
        return;
    initNotificationBell();
    const cachedHub = loadSessionCache(HUB_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (cachedHub) {
        applyHubData(cachedHub);
        renderSummary(hubData.repos || []);
        renderReposWithScroll(hubData.repos || []);
        renderAgentWorkspaces(hubData.agent_workspaces || []);
    }
    const cachedUsage = loadSessionCache(HUB_USAGE_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (cachedUsage) {
        indexHubUsage(cachedUsage);
        renderHubUsageMeta(cachedUsage);
    }
    loadHubChannelDirectory({ silent: true }).catch(() => { });
    refreshHub();
    loadHubVersion();
    checkUpdateStatus();
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
export const __hubTest = {
    renderRepos,
    renderAgentWorkspaces,
    applyHubPanelState,
    toggleHubPanel,
    initInteractionHarness() {
        attachHubHandlers();
        initHubPanelControls();
    },
    setHubChannelEntries(entries) {
        hubChannelEntries = Array.isArray(entries) ? [...entries] : [];
    },
};
