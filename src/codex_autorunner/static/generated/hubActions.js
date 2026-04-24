// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { normalizePinnedParentRepoIds } from "./hubFilters.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
export const HUB_JOB_POLL_INTERVAL_MS = 1200;
export const HUB_JOB_TIMEOUT_MS = 180000;
export let hubData = {
    repos: [],
    agent_workspaces: [],
    last_scan_at: null,
    pinned_parent_repo_ids: [],
};
export let pinnedParentRepoIds = new Set();
let hubChannelEntries = [];
export function getHubData() {
    return hubData;
}
export function getHubChannelEntries() {
    return hubChannelEntries;
}
export function setHubChannelEntries(entries) {
    hubChannelEntries = Array.isArray(entries) ? [...entries] : [];
}
export function getPinnedParentRepoIds() {
    return pinnedParentRepoIds;
}
export function setPinnedParentRepoIds(ids, data) {
    pinnedParentRepoIds = ids;
    data.pinned_parent_repo_ids = Array.from(ids);
}
export function applyHubData(data) {
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
export async function startHubJob(path, { body, startedMessage } = {}) {
    const job = await api(path, { method: "POST", body });
    if (startedMessage) {
        flash(startedMessage);
    }
    return pollHubJob(job.job_id);
}
export { applyHubPanelState, toggleHubPanel, initInteractionHarness, attachHandlersAndControls } from "./hubDomBindings.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
export { bootstrapHubData, refreshHub, triggerHubScan, loadHubUsage, handleSystemUpdate } from "./hubRefresh.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
export { handleCleanupAll, showCreateRepoModal, showCreateAgentWorkspaceModal, hideCreateRepoModal, hideCreateAgentWorkspaceModal, handleCreateRepoSubmit, handleCreateAgentWorkspaceSubmit, initHubSettings, } from "./hubModals.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
