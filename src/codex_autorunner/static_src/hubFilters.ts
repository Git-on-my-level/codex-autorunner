import { HUB_BASE } from "./env.js";
import type {
  HubRepo,
  HubChannelEntry,
  HubFlowFilter,
  HubSortOrder,
  HubViewPrefs,
  HubRepoGroup,
} from "./hubTypes.js";

export function nonPmaChatBoundThreadCount(repo: HubRepo): number {
  if (repo.non_pma_chat_bound_thread_count != null) {
    return Math.max(0, Number(repo.non_pma_chat_bound_thread_count || 0));
  }
  const totalCount = Number(repo.chat_bound_thread_count || 0);
  const pmaCount = Number(repo.pma_chat_bound_thread_count || 0);
  return Math.max(0, totalCount - pmaCount);
}

export function isCleanupBlockedByChatBinding(repo: HubRepo): boolean {
  if ((repo.kind || "base") !== "worktree") return false;
  if (repo.cleanup_blocked_by_chat_binding === true) return true;
  return nonPmaChatBoundThreadCount(repo) > 0;
}

export function isChatBoundWorktree(repo: HubRepo): boolean {
  return isCleanupBlockedByChatBinding(repo);
}

export function unboundManagedThreadCount(repo: HubRepo): number {
  return Math.max(0, Number(repo.unbound_managed_thread_count || 0));
}

export function inferBaseId(repo: HubRepo | null): string | null {
  if (!repo) return null;
  if (repo.worktree_of) return repo.worktree_of;
  if (typeof repo.id === "string" && repo.id.includes("--")) {
    return repo.id.split("--")[0];
  }
  return null;
}

export function repoLastActivityMs(repo: HubRepo): number {
  const raw = repo.last_run_finished_at || repo.last_run_started_at;
  if (!raw) return 0;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function repoFlowStatus(repo: HubRepo): string {
  const status = repo.ticket_flow_display?.status || repo.ticket_flow?.status || "idle";
  return String(status || "idle").toLowerCase();
}

export function repoFlowProgress(repo: HubRepo): number {
  const done = Number(repo.ticket_flow_display?.done_count || repo.ticket_flow?.done_count || 0);
  const total = Number(repo.ticket_flow_display?.total_count || repo.ticket_flow?.total_count || 0);
  if (total <= 0) return 0;
  return done / total;
}

export function repoMatchesFlowFilter(repo: HubRepo, filter: HubFlowFilter): boolean {
  if (filter === "all") return true;
  const flowStatus = repoFlowStatus(repo);
  if (filter === "active") {
    return (
      flowStatus === "running" ||
      flowStatus === "pending" ||
      flowStatus === "paused" ||
      flowStatus === "stopping"
    );
  }
  if (filter === "running") return flowStatus === "running";
  if (filter === "paused") return flowStatus === "paused";
  if (filter === "completed") return flowStatus === "completed" || flowStatus === "done";
  if (filter === "failed") {
    return (
      flowStatus === "failed" ||
      flowStatus === "stopped" ||
      flowStatus === "superseded"
    );
  }
  return flowStatus === "idle";
}

export function compareReposForSort(a: HubRepo, b: HubRepo, sortOrder: HubSortOrder): number {
  if (sortOrder === "last_activity_desc") {
    return (
      repoLastActivityMs(b) - repoLastActivityMs(a) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  if (sortOrder === "last_activity_asc") {
    return (
      repoLastActivityMs(a) - repoLastActivityMs(b) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  if (sortOrder === "flow_progress_desc") {
    return (
      repoFlowProgress(b) - repoFlowProgress(a) ||
      repoLastActivityMs(b) - repoLastActivityMs(a) ||
      String(a.id).localeCompare(String(b.id))
    );
  }
  return String(a.id).localeCompare(String(b.id));
}

export function normalizedHubSearch(): string {
  const hubRepoSearchInput = document.getElementById(
    "hub-repo-search"
  ) as HTMLInputElement | null;
  return String(hubRepoSearchInput?.value || "").trim().toLowerCase();
}

export function repoSearchBlob(repo: HubRepo): string {
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

export function repoMatchesSearch(repo: HubRepo, query: string): boolean {
  if (!query) return true;
  return repoSearchBlob(repo).includes(query);
}

export function channelSearchBlob(channel: HubChannelEntry): string {
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

export function channelMatchesSearch(channel: HubChannelEntry, query: string): boolean {
  if (!query) return true;
  return channelSearchBlob(channel).includes(query);
}

export const HUB_VIEW_PREFS_KEY = `car:hub-view-prefs:${HUB_BASE || "/"}`;
export const HUB_DEFAULT_VIEW_PREFS: HubViewPrefs = {
  flowFilter: "all",
  sortOrder: "repo_id",
};

export const hubViewPrefs: HubViewPrefs = { ...HUB_DEFAULT_VIEW_PREFS };

export function saveHubViewPrefs(): void {
  try {
    localStorage.setItem(HUB_VIEW_PREFS_KEY, JSON.stringify(hubViewPrefs));
  } catch (_err) {
    // Ignore local storage failures; prefs are best-effort.
  }
}

export const HUB_PANEL_PREFS_KEY = `car:hub-open-panel:${HUB_BASE || "/"}`;

export function saveHubOpenPanel(value: string): void {
  try {
    localStorage.setItem(HUB_PANEL_PREFS_KEY, value);
  } catch (_err) {
    // Ignore local storage failures; prefs are best-effort.
  }
}

export function loadHubOpenPanel(): string {
  try {
    const raw = localStorage.getItem(HUB_PANEL_PREFS_KEY);
    if (raw === "repos" || raw === "agents") {
      return raw;
    }
  } catch (_err) {
    // Ignore parse/storage errors; defaults apply.
  }
  return "repos";
}

export function loadHubViewPrefs(): void {
  try {
    const raw = localStorage.getItem(HUB_VIEW_PREFS_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as Partial<HubViewPrefs>;
    const flowFilter = parsed.flowFilter;
    const sortOrder = parsed.sortOrder;
    if (
      flowFilter === "all" ||
      flowFilter === "active" ||
      flowFilter === "running" ||
      flowFilter === "paused" ||
      flowFilter === "completed" ||
      flowFilter === "failed" ||
      flowFilter === "idle"
    ) {
      hubViewPrefs.flowFilter = flowFilter;
    }
    if (
      sortOrder === "repo_id" ||
      sortOrder === "last_activity_desc" ||
      sortOrder === "last_activity_asc" ||
      sortOrder === "flow_progress_desc"
    ) {
      hubViewPrefs.sortOrder = sortOrder;
    }
  } catch (_err) {
    // Ignore parse/storage errors; defaults apply.
  }
}

export function normalizePinnedParentRepoIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  value.forEach((entry) => {
    if (typeof entry !== "string") return;
    const repoId = entry.trim();
    if (!repoId || seen.has(repoId)) return;
    seen.add(repoId);
    out.push(repoId);
  });
  return out;
}

export function buildRepoGroups(
  repos: HubRepo[],
  pinnedParentRepoIds: Set<string>
): {
  groups: HubRepoGroup[];
  orphanWorktrees: HubRepo[];
  chatBoundWorktrees: HubRepo[];
} {
  const bases = repos.filter((r) => (r.kind || "base") === "base");
  const allWorktrees = repos.filter((r) => (r.kind || "base") === "worktree");
  const chatBoundWorktrees: HubRepo[] = [];
  const worktrees: HubRepo[] = [];
  allWorktrees.forEach((repo) => {
    if (isChatBoundWorktree(repo)) {
      chatBoundWorktrees.push(repo);
      return;
    }
    worktrees.push(repo);
  });
  const byBase = new Map<string, { base: HubRepo; worktrees: HubRepo[] }>();
  bases.forEach((b) => byBase.set(b.id, { base: b, worktrees: [] }));

  const orphanWorktrees: HubRepo[] = [];
  worktrees.forEach((w) => {
    const baseId = inferBaseId(w);
    if (baseId && byBase.has(baseId)) {
      byBase.get(baseId)!.worktrees.push(w);
    } else {
      orphanWorktrees.push(w);
    }
  });

  const groups: HubRepoGroup[] = [...byBase.values()].map((group) => {
    const filteredWorktrees =
      hubViewPrefs.flowFilter === "all"
        ? [...group.worktrees]
        : group.worktrees.filter((repo) =>
            repoMatchesFlowFilter(repo, hubViewPrefs.flowFilter)
          );
    const baseMatches = repoMatchesFlowFilter(group.base, hubViewPrefs.flowFilter);
    const matchesFilter =
      hubViewPrefs.flowFilter === "all" || baseMatches || filteredWorktrees.length > 0;
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

function formatDestinationSummary(
  destination: Record<string, unknown> | null | undefined
): string {
  if (!destination || typeof destination !== "object") return "local";
  const kindRaw = destination.kind;
  const kind = typeof kindRaw === "string" ? kindRaw.trim().toLowerCase() : "local";
  if (kind === "docker") {
    const image = typeof destination.image === "string" ? destination.image.trim() : "";
    return image ? `docker:${image}` : "docker";
  }
  return "local";
}
