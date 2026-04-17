// GENERATED FILE - do not edit directly. Source: static_src/
import { escapeHtml, statusPill, resolvePath } from "./utils.js";
import { preserveScroll } from "./preserve.js";
import { formatTokensCompact, getRepoUsage, getHubUsageUnmatched } from "./hubCache.js";
import { buildRepoGroups, compareReposForSort, hubViewPrefs, isCleanupBlockedByChatBinding, normalizedHubSearch, repoFlowStatus, repoMatchesFlowFilter, repoMatchesSearch, channelMatchesSearch, unboundManagedThreadCount, } from "./hubFilters.js";
let cleanupAllInFlight = false;
export function setCleanupAllInFlight(value) {
    cleanupAllInFlight = value;
}
const repoListEl = document.getElementById("hub-repo-list");
const agentWorkspaceListEl = document.getElementById("hub-agent-workspace-list");
const lastScanEl = document.getElementById("hub-last-scan");
const pmaLastScanEl = document.getElementById("pma-last-scan");
const totalEl = document.getElementById("hub-count-total");
const runningEl = document.getElementById("hub-count-running");
const missingEl = document.getElementById("hub-count-missing");
function getHubCleanupAllBtn() {
    return document.getElementById("hub-cleanup-all");
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
        return `<span class="${className} hub-mount-pill">${escapeHtml(label)}</span>`;
    }
    else {
        return "";
    }
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
    return `<span class="${className} hub-mount-pill"${titleAttr}>${escapeHtml(label)}</span>`;
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
function toPositiveInt(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0)
        return null;
    return Math.floor(parsed);
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
    return visibleChannels.some((ch) => {
        const channelKey = String(ch.key || "")
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
function updateCleanupAllButton(repos) {
    const btn = getHubCleanupAllBtn();
    if (!btn)
        return;
    const hasUnboundBaseThreads = repos.some((r) => (r.kind || "base") === "base" && unboundManagedThreadCount(r) > 0);
    const hasEligibleWorktree = repos.some((r) => r.kind === "worktree" &&
        !isCleanupBlockedByChatBinding(r) &&
        r.is_clean !== false);
    const hasCompletedFlowCleanupHint = repos.some((r) => {
        const s = repoFlowStatus(r);
        return s === "completed" || s === "done";
    });
    const active = hasUnboundBaseThreads || hasEligibleWorktree || hasCompletedFlowCleanupHint;
    const empty = !active;
    btn.textContent = "Cleanup all";
    if (cleanupAllInFlight) {
        btn.disabled = true;
        btn.classList.remove("hub-cleanup-all--empty");
        btn.removeAttribute("aria-disabled");
        btn.title = "Cleaning up…";
        return;
    }
    btn.disabled = false;
    if (empty) {
        btn.setAttribute("aria-disabled", "true");
    }
    else {
        btn.removeAttribute("aria-disabled");
    }
    btn.classList.toggle("hub-cleanup-all--empty", empty);
    btn.title = empty
        ? "No unbound threads, eligible worktrees, or completed flows in hub data (click for full preview)"
        : "Clean slate: archive stale threads, eligible worktrees, and completed flow runs";
}
export function renderSummary(repos, hubData) {
    const running = repos.filter((r) => r.status === "running").length;
    const missing = repos.filter((r) => !r.exists_on_disk).length;
    updateCleanupAllButton(repos);
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
export function renderRepos(repos, hubChannelEntries, pinnedParentRepoIds) {
    if (!repoListEl)
        return;
    repoListEl.innerHTML = "";
    updateCleanupAllButton(repos);
    const searchQuery = normalizedHubSearch();
    const repoChannels = channelsByRepoId(hubChannelEntries);
    if (!repos.length) {
        repoListEl.innerHTML = '<div class="hub-empty muted">No repos found.</div>';
        return;
    }
    const { groups, orphanWorktrees, chatBoundWorktrees } = buildRepoGroups(repos, pinnedParentRepoIds);
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
    const hubUsageUnmatchedVal = getHubUsageUnmatched();
    if (hubUsageUnmatchedVal && hubUsageUnmatchedVal.events) {
        const note = document.createElement("div");
        note.className = "hub-usage-unmatched-note muted small";
        const total = formatTokensCompact(hubUsageUnmatchedVal.totals?.total_tokens);
        note.textContent = `Other: ${total} · ${hubUsageUnmatchedVal.events}ev (unattributed)`;
        repoListEl.appendChild(note);
    }
}
export function renderReposWithScroll(repos, hubChannelEntries, pinnedParentRepoIds) {
    preserveScroll(repoListEl, () => {
        renderRepos(repos, hubChannelEntries, pinnedParentRepoIds);
    }, { restoreOnNextFrame: true });
}
export function renderAgentWorkspaces(agentWorkspaces, hubChannelEntries) {
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
