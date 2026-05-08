// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, inputModal, confirmModal } from "./utils.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { normalizePinnedParentRepoIds, isCleanupBlockedByChatBinding, unboundManagedThreadCount, saveHubOpenPanel, saveHubViewPrefs, hubViewPrefs, loadHubViewPrefs, loadHubOpenPanel, } from "./hubFilters.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { initNotificationBell } from "./notificationBell.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { importVersionedModule } from "./assetLoader.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { renderReposWithScroll } from "./hubRepoCards.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { getHubData, getHubChannelEntries, getPinnedParentRepoIds, setPinnedParentRepoIds, } from "./hubActions.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { refreshHub, triggerHubScan, loadHubUsage } from "./hubRefresh.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
import { openRepoSettingsModal, promptAndSetRepoDestination, handleCleanupAll, showCreateRepoModal, hideCreateRepoModal, handleCreateRepoSubmit, initHubSettings, } from "./hubModals.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
const hubRepoSearchInput = document.getElementById("hub-repo-search");
const hubFlowFilterEl = document.getElementById("hub-flow-filter");
const hubSortOrderEl = document.getElementById("hub-sort-order");
const hubRepoPanelEl = document.getElementById("hub-repo-panel");
const hubShellEl = document.getElementById("hub-shell");
const hubRepoPanelSummaryEl = document.getElementById("hub-repo-panel-summary");
const hubRepoPanelStateEl = document.getElementById("hub-repo-panel-state");
const repoListEl = document.getElementById("hub-repo-list");
const hubUsageRefresh = document.getElementById("hub-usage-refresh");
let hubOpenPanel = loadHubOpenPanel();
let hubCleanupAllClickBound = false;
const prefetchedUrls = new Set();
function resolvePath(path) {
    const base = window.__CAR_BASE_PREFIX || "";
    if (!base || path.startsWith(base))
        return path;
    return `${base}${path}`;
}
async function setParentRepoPinned(repoId, pinned) {
    const response = await api(`/hub/repos/${encodeURIComponent(repoId)}/pin`, {
        method: "POST",
        body: { pinned },
    });
    const ids = new Set(normalizePinnedParentRepoIds(response?.pinned_parent_repo_ids));
    setPinnedParentRepoIds(ids, getHubData());
}
async function handleRepoAction(repoId, action) {
    const buttons = repoListEl?.querySelectorAll(`button[data-repo="${repoId}"][data-action="${action}"]`);
    buttons?.forEach((btn) => btn.disabled = true);
    try {
        if (action === "pin_parent" || action === "unpin_parent") {
            const pinned = action === "pin_parent";
            await setParentRepoPinned(repoId, pinned);
            const hubData = getHubData();
            renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
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
            const { startHubJob } = await importVersionedModule("./hubActions.js");
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
            const repo = getHubData().repos.find((item) => item.id === repoId);
            if (!repo) {
                flash(`Repo not found: ${repoId}`, "error");
                return;
            }
            await openRepoSettingsModal(repo);
            return;
        }
        if (action === "set_destination") {
            const repo = getHubData().repos.find((item) => item.id === repoId);
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
            const repo = getHubData().repos.find((item) => item.id === repoId);
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
            const { startHubJob } = await importVersionedModule("./hubActions.js");
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
            const repo = getHubData().repos.find((item) => item.id === repoId);
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
            const { removeRepoWithChecks } = await importVersionedModule("./hubModals.js");
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
function prefetchRepo(url) {
    if (!url || prefetchedUrls.has(url))
        return;
    prefetchedUrls.add(url);
    fetch(url, { method: "GET", headers: { "x-prefetch": "1" } }).catch(() => { });
}
export function applyHubPanelState(openPanel) {
    hubOpenPanel = openPanel;
    const reposOpen = openPanel === "repos";
    hubShellEl?.setAttribute("data-hub-open-panel", openPanel);
    hubRepoPanelEl?.classList.toggle("hub-panel-expanded", reposOpen);
    hubRepoPanelEl?.classList.toggle("hub-panel-collapsed", !reposOpen);
    if (hubRepoPanelSummaryEl) {
        hubRepoPanelSummaryEl.setAttribute("aria-expanded", reposOpen ? "true" : "false");
    }
    if (hubRepoPanelStateEl) {
        hubRepoPanelStateEl.textContent = reposOpen ? "Expanded" : "Show panel";
    }
}
export function toggleHubPanel(panel) {
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
}
function initHubRepoListControls() {
    loadHubViewPrefs();
    const hubData = getHubData();
    if (hubFlowFilterEl) {
        hubFlowFilterEl.value = hubViewPrefs.flowFilter;
        hubFlowFilterEl.addEventListener("change", () => {
            hubViewPrefs.flowFilter = hubFlowFilterEl.value;
            saveHubViewPrefs();
            renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        });
    }
    if (hubSortOrderEl) {
        hubSortOrderEl.value = hubViewPrefs.sortOrder;
        hubSortOrderEl.addEventListener("change", () => {
            hubViewPrefs.sortOrder = hubSortOrderEl.value;
            saveHubViewPrefs();
            renderReposWithScroll(hubData.repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        });
    }
}
function attachHubHandlers() {
    initHubSettings();
    const refreshBtn = document.getElementById("hub-refresh");
    const newRepoBtn = document.getElementById("hub-new-repo");
    const createCancelBtn = document.getElementById("create-repo-cancel");
    const createSubmitBtn = document.getElementById("create-repo-submit");
    const createRepoId = document.getElementById("create-repo-id");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", () => triggerHubScan());
    }
    if (hubUsageRefresh) {
        hubUsageRefresh.addEventListener("click", () => loadHubUsage());
    }
    const cleanupAllBtn = document.getElementById("hub-cleanup-all");
    if (cleanupAllBtn) {
        if (!hubCleanupAllClickBound) {
            hubCleanupAllClickBound = true;
            cleanupAllBtn.addEventListener("click", () => {
                void handleCleanupAll();
            });
        }
    }
    else {
        console.warn("hub-cleanup-all button not found in DOM");
    }
    if (hubRepoSearchInput) {
        hubRepoSearchInput.addEventListener("input", () => {
            renderReposWithScroll(getHubData().repos || [], getHubChannelEntries(), getPinnedParentRepoIds());
        });
    }
    if (newRepoBtn) {
        newRepoBtn.addEventListener("click", () => {
            showCreateRepoModal();
        });
    }
    if (createCancelBtn) {
        createCancelBtn.addEventListener("click", hideCreateRepoModal);
    }
    if (createSubmitBtn) {
        createSubmitBtn.addEventListener("click", handleCreateRepoSubmit);
    }
    if (createRepoId) {
        createRepoId.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleCreateRepoSubmit();
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
}
export function initInteractionHarness() {
    attachHubHandlers();
    initHubPanelControls();
}
export function attachHandlersAndControls() {
    attachHubHandlers();
    initHubRepoListControls();
    initHubPanelControls();
    initNotificationBell();
}
