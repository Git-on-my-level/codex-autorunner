import { mapSurfaceArtifact } from './domain';
import { renderMarkdownToHtml } from './markdown';
import type {
  GitStatusSummary,
  PmaChatSummary,
  PmaRunProgress,
  RepoSummary,
  SurfaceArtifact,
  TicketSummary,
  WorkStatus,
  WorktreeSummary,
  ContextspaceDocument
} from './domain';
import { buildPmaChatListEntries, formatRelativeTime, pmaChatKind, pmaChatKindLabel, progressPercent, statusLabel } from './pmaChat';
import type { PmaChatKind, PmaChatRunGroup } from './pmaChat';
import { repoContextspaceRoute, repoRoute, repoTicketRoute, worktreeContextspaceRoute, worktreeRoute, worktreeTicketRoute } from './routes';
import {
  aliasesOverlap,
  buildTicketFlowStatusViewModel,
  ticketAliases,
  ticketAliasesFromRun,
  type TicketFlowStatusViewModel
} from './ticketFlowStatus';

export type RepoWorktreeKind = 'repo' | 'worktree';
export type RepoWorktreeIndexFilter = 'all' | 'active' | 'waiting';

export type RepoWorktreeIndexRow = {
  id: string;
  kind: RepoWorktreeKind;
  label: string;
  detail: string | null;
  status: WorkStatus;
  branch: string | null;
  path: string | null;
  activeRuns: number;
  openTickets: number;
  totalTickets: number;
  doneTickets: number;
  lastActivityAt: string | null;
  href: string;
  ticketHref: string | null;
  repoHref: string | null;
  childWorktrees: RepoWorktreeChildRow[];
  /** PMA chats + ticket-flow runs scoped to this row (runs tied to an already-counted chat are skipped). */
  signalWaiting: number;
  signalFailed: number;
  signalActive: number;
  /** Deep-link into chats with the new-chat scope picker preset for PMA mediation. */
  pmaChatHref: string;
  /** Deep-link into chats with the new-chat scope picker preset for direct agent control. */
  codingAgentChatHref: string;
  hasCarState: boolean;
  unboundManagedThreadCount: number;
  chatBound: boolean;
  cleanupBlockedByChatBinding: boolean;
  /** Total worktree children for this repo (zero for worktree rows). */
  totalWorktrees: number;
  /** Worktrees considered "in use": dirty, running, or with active/waiting/failed signals. */
  inUseWorktrees: number;
  /** Subset of in-use that are dirty (used for tooltip detail). */
  dirtyWorktrees: number;
};

export type RepoWorktreeChildRow = {
  id: string;
  label: string;
  status: WorkStatus;
  branch: string | null;
  path: string | null;
  activeRuns: number;
  openTickets: number;
  totalTickets: number;
  doneTickets: number;
  currentRunTitle: string | null;
  currentTicketId: string | null;
  lastActivityAt: string | null;
  href: string;
  ticketHref: string | null;
  /** Deep-link into chats with PMA mediation scoped to this worktree. */
  pmaChatHref: string;
  /** Deep-link into chats with direct agent control scoped to this worktree. */
  codingAgentChatHref: string;
  /** PMA chats + ticket-flow runs scoped to this worktree. */
  signalWaiting: number;
  signalFailed: number;
  signalActive: number;
  hasCarState: boolean;
  unboundManagedThreadCount: number;
  chatBound: boolean;
  cleanupBlockedByChatBinding: boolean;
};

export type RepoWorktreeRunCard = {
  id: string;
  title: string;
  status: WorkStatus;
  phase: string | null;
  agentId: string | null;
  progress: number | null;
  updatedAt: string | null;
  ticketId: string | null;
  chatHref: string | null;
  ticketHref: string | null;
};

export type RepoWorktreeTicketRow = {
  id: string;
  title: string;
  status: WorkStatus;
  href: string;
  diffStats: TicketSummary['diffStats'];
  durationLabel: string | null;
  bodyPreview: string | null;
  isCurrent: boolean;
};

export type RepoWorktreeChatRunGroup = {
  key: string;
  scopeKind: 'worktree' | 'repo';
  scopeLabel: string;
  status: WorkStatus;
  totalCount: number;
  activeCount: number;
  waitingCount: number;
  doneCount: number;
  failedCount: number;
  agents: string[];
  updatedAt: string | null;
  chats: RepoWorktreeChatRow[];
  /** Single deep link to the chats page filtered/preselected to this group's first chat. */
  href: string;
};

export type RepoWorktreeChatList = {
  groups: RepoWorktreeChatRunGroup[];
  standaloneChats: RepoWorktreeChatRow[];
  totalChatCount: number;
};

export type RepoWorktreeChatRow = {
  id: string;
  title: string;
  status: WorkStatus;
  kind: PmaChatKind;
  kindLabel: string;
  agentId: string | null;
  model: string | null;
  updatedAt: string | null;
  href: string;
  /** Ticket id when this chat was spawned by ticket flow; null for ad-hoc chats. */
  ticketId: string | null;
};

export type RepoWorktreeArtifactRow = {
  id: string;
  title: string;
  summary: string;
  kind: SurfaceArtifact['kind'];
  href: string | null;
  createdAt: string | null;
};

export type RepoWorktreeContextspaceRow = {
  id: string;
  title: string;
  filename: string;
  summary: string;
  status: 'present' | 'empty';
  updatedAt: string | null;
  href: string;
  /** Trimmed multi-line preview; non-empty only when the doc warrants inline expansion (e.g. spec). */
  preview: string | null;
  /** Rendered markdown HTML for inline previews. */
  previewHtml: string | null;
};

export type RepoWorktreeLink = {
  label: string;
  href: string;
  secondary: boolean;
};

export type RepoWorktreeIndexViewModel = {
  title: string;
  eyebrow: string;
  rows: RepoWorktreeIndexRow[];
  activeCount: number;
  waitingCount: number;
  openTicketCount: number;
  /**
   * When false, ticket quantity chips and progress are hidden (hub ticket list did not load).
   * Counts on rows are zero; single source of truth is `/hub/tickets` when true.
   */
  ticketIndexMetricsAvailable: boolean;
};

export function filterRepoWorktreeIndexRows(
  rows: RepoWorktreeIndexRow[],
  search: string,
  filter: RepoWorktreeIndexFilter
): RepoWorktreeIndexRow[] {
  const needle = search.trim().toLowerCase();
  return rows.filter((row) => {
    const rowMatches = rowMatchesNeedle(row, needle) && rowMatchesFilter(row, filter);
    const childMatches = row.childWorktrees.some(
      (child) => childMatchesNeedle(child, needle) && childMatchesFilter(child, filter)
    );
    return rowMatches || childMatches;
  });
}

export function visibleRepoWorktreeChildren(
  row: RepoWorktreeIndexRow,
  search: string,
  filter: RepoWorktreeIndexFilter
): RepoWorktreeChildRow[] {
  const needle = search.trim().toLowerCase();
  const repoMatches = rowMatchesNeedle(row, needle) && rowMatchesFilter(row, filter);
  if (repoMatches) return row.childWorktrees.filter((child) => childMatchesFilter(child, filter));
  return row.childWorktrees.filter((child) => childMatchesNeedle(child, needle) && childMatchesFilter(child, filter));
}

export function countRepoWorktreeIndexEntities(
  rows: RepoWorktreeIndexRow[],
  filter: RepoWorktreeIndexFilter = 'all'
): number {
  return rows.reduce((total, row) => {
    const rowCount = rowMatchesFilter(row, filter) ? 1 : 0;
    const childCount = row.childWorktrees.filter((child) => childMatchesFilter(child, filter)).length;
    return total + rowCount + childCount;
  }, 0);
}

export type RepoWorktreeDetailViewModel = {
  kind: RepoWorktreeKind;
  id: string;
  isMissing: boolean;
  title: string;
  eyebrow: string;
  branch: string | null;
  path: string | null;
  stateLabel: string;
  currentRuns: RepoWorktreeRunCard[];
  flowStatus: TicketFlowStatusViewModel;
  activity: RepoWorktreeArtifactRow[];
  chats: RepoWorktreeChatRow[];
  /** Chats grouped by ticket-flow run for the new collapsed-by-default panel. */
  chatList: RepoWorktreeChatList;
  contextspace: RepoWorktreeContextspaceRow[];
  contextspaceHref: string;
  currentTickets: RepoWorktreeTicketRow[];
  nextTickets: RepoWorktreeTicketRow[];
  artifacts: RepoWorktreeArtifactRow[];
  links: RepoWorktreeLink[];
  ticketIndexHref: string;
  ticketIndexLabel: string;
  /** Compact rollup of ticket queue counts for the overview card. */
  ticketOverview: RepoWorktreeTicketOverview;
  childWorktrees: RepoWorktreeChildRow[];
  baseRepoLabel: string | null;
  baseRepoHref: string | null;
  hasActiveRun: boolean;
  missingIndexHref: string;
  missingIndexLabel: string;
  pmaChatHref: string;
  codingAgentChatHref: string;
  gitStatus: GitStatusSummary | null;
  hasCarState: boolean;
  unboundManagedThreadCount: number;
  chatBound: boolean;
  cleanupBlockedByChatBinding: boolean;
};

export type RepoWorktreeTicketOverview = {
  total: number;
  done: number;
  open: number;
  active: number;
  failed: number;
  /** Up to 3 representative open tickets (current first, then queued). */
  preview: RepoWorktreeTicketRow[];
  /** Number of remaining open tickets not in `preview`. */
  remaining: number;
};

export type RepoWorktreeSourceData = {
  repos: RepoSummary[];
  worktrees: WorktreeSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  tickets: TicketSummary[];
  contextspaceDocs?: ContextspaceDocument[];
  artifacts: SurfaceArtifact[];
  /**
   * Set from `tickets.ok` on index pages. When `false`, index ticket chips are suppressed (no snapshot fallback).
   */
  ticketsListLoaded?: boolean;
};

export function buildRepoWorktreeIndexViewModel(
  source: RepoWorktreeSourceData,
  kind: 'all' | RepoWorktreeKind = 'all'
): RepoWorktreeIndexViewModel {
  const ticketIndexMetricsAvailable = source.ticketsListLoaded !== false;
  const repoIds = new Set(source.repos.map((repo) => repo.id));
  const orphanWorktrees = source.worktrees.filter((worktree) => !worktree.repoId || !repoIds.has(worktree.repoId));
  const rows =
    kind === 'worktree'
      ? source.worktrees
          .map((worktree) => enrichIndexRowSignals(worktreeToIndexRow(worktree, source), source))
          .sort(bySignalsThenActiveThenRecent)
      : [
          ...source.repos.map((repo) =>
            enrichIndexRowSignals(
              repoToIndexRow(
                repo,
                source.worktrees.filter((worktree) => worktree.repoId === repo.id),
                source
              ),
              source
            )
          ),
          ...(kind === 'all'
            ? orphanWorktrees.map((worktree) =>
                enrichIndexRowSignals(worktreeToIndexRow(worktree, source), source)
              )
            : [])
        ].sort(bySignalsThenActiveThenRecent);
  return {
    title: kind === 'worktree' ? 'Secondary worktree index' : 'Repos',
    eyebrow: kind === 'worktree' ? 'Repo-owned variants' : 'Repo ownership',
    rows,
    activeCount: countRepoWorktreeIndexEntities(rows, 'active'),
    waitingCount: countRepoWorktreeIndexEntities(rows, 'waiting'),
    ticketIndexMetricsAvailable,
    openTicketCount: ticketIndexMetricsAvailable
      ? rows.reduce(
          (total, row) =>
            total + row.openTickets + row.childWorktrees.reduce((childTotal, child) => childTotal + child.openTickets, 0),
          0
        )
      : 0
  };
}

export function buildRepoWorktreeDetailViewModel(
  source: RepoWorktreeSourceData,
  kind: RepoWorktreeKind,
  id: string
): RepoWorktreeDetailViewModel {
  const resource =
    kind === 'repo'
      ? source.repos.find((repo) => repo.id === id) ?? null
      : source.worktrees.find((worktree) => worktree.id === id) ?? null;
  if (!resource) return missingDetailViewModel(kind, id);
  const title = resource?.name ?? id;
  const branch = kind === 'repo' ? (resource as RepoSummary | null)?.defaultBranch ?? null : (resource as WorktreeSummary | null)?.branch ?? null;
  const path = resource?.path ?? null;
  const childWorktreeSummaries = kind === 'repo' ? source.worktrees.filter((worktree) => worktree.repoId === id) : [];
  const baseRepo =
    kind === 'worktree'
      ? source.repos.find((repo) => repo.id === (resource as WorktreeSummary | null)?.repoId) ?? null
      : null;
  const parentRepoId = kind === 'worktree' ? (resource as WorktreeSummary | null)?.repoId ?? null : null;
  const primaryRuns = source.runs.filter((run) => runMatchesResource(run, kind, id));
  const primaryChats = source.chats.filter((chat) => chatMatchesResource(chat, kind, id));
  const runCards = mergeRunCards(primaryRuns, primaryChats, kind, id, parentRepoId);
  const activeRunCards = runCards.filter((run) => ['running', 'waiting', 'blocked'].includes(run.status));
  const visibleRuns = activeRunCards.length ? activeRunCards : runCards.slice(0, 1);
  const currentTicketIds = new Set(visibleRuns.map((run) => run.ticketId).filter((ticketId): ticketId is string => Boolean(ticketId)));
  const scopedTickets = ticketsForResource(source.tickets, kind, id);
  const flowStatus = buildTicketFlowStatusViewModel(scopedTickets, primaryRuns, { kind, id });
  const activeCurrentTicketId = isActiveTicketFlowStatus(flowStatus.status) ? flowStatus.currentTicketId : null;
  const currentTickets = ticketsForIds(scopedTickets, currentTicketIds, activeCurrentTicketId);
  const nextTickets = scopedTickets
    .filter((ticket) => ticket.status !== 'done' && !currentTicketIds.has(ticket.id))
    .slice(0, 5)
    .map((ticket) => ticketToRow(ticket, activeCurrentTicketId));
  const ticketOverview = buildTicketOverview(scopedTickets, currentTickets, nextTickets);
  const resourceArtifacts = asRecordArray(resource.raw.current_run_artifacts).map(mapSurfaceArtifact);
  const runArtifacts = [...resourceArtifacts, ...source.artifacts, ...primaryRuns.flatMap((run) => run.events)].map(artifactToRow);
  const activity = [
    ...primaryRuns.flatMap((run) => run.events).map(artifactToRow),
    ...visibleRuns.map(runToActivity)
  ].slice(0, 6);

  return {
    kind,
    id,
    isMissing: false,
    title,
    eyebrow: kind === 'repo' ? 'Repo current run' : 'Repo worktree current run',
    branch,
    path,
    stateLabel: statusLabel(resource?.status ?? visibleRuns[0]?.status ?? 'idle'),
    currentRuns: visibleRuns,
    flowStatus,
    activity,
    chats: primaryChats
      .map((chat) => chatToRow(chat))
      .sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? '')),
    chatList: buildScopedChatList(primaryChats, kind, id),
    contextspaceHref: kind === 'repo' ? repoContextspaceRoute(id) : worktreeContextspaceRoute(id, parentRepoId),
    contextspace: contextspaceRows(
      source.contextspaceDocs ?? [],
      kind === 'repo' ? repoContextspaceRoute(id) : worktreeContextspaceRoute(id, parentRepoId)
    ),
    currentTickets,
    nextTickets,
    artifacts: runArtifacts.slice(0, 6),
    links: buildContextLinks(kind, id, runArtifacts, parentRepoId),
    ticketIndexHref: scopedTicketHref(kind, id, parentRepoId),
    ticketIndexLabel: kind === 'repo' ? 'Repo tickets' : 'Worktree tickets',
    ticketOverview,
    childWorktrees: childWorktreeSummaries.map((worktree) => worktreeToNavChildRow(worktree, baseRepo?.name ?? null)),
    baseRepoLabel: baseRepo?.name ?? (kind === 'worktree' ? (resource as WorktreeSummary | null)?.repoId ?? null : null),
    baseRepoHref: kind === 'worktree' && (resource as WorktreeSummary | null)?.repoId ? repoRoute((resource as WorktreeSummary).repoId as string) : null,
    hasActiveRun: activeRunCards.length > 0,
    missingIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    missingIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees',
    pmaChatHref: scopedChatHref(kind, id, 'pma'),
    codingAgentChatHref: scopedChatHref(kind, id, 'agent'),
    gitStatus: resource.gitStatus ?? null,
    hasCarState: boolFromRaw(resource.raw, 'has_car_state'),
    unboundManagedThreadCount: numberFromRaw(resource.raw, 'unbound_managed_thread_count'),
    chatBound: boolFromRaw(resource.raw, 'chat_bound'),
    cleanupBlockedByChatBinding: boolFromRaw(resource.raw, 'cleanup_blocked_by_chat_binding')
  };
}

function buildTicketOverview(
  tickets: TicketSummary[],
  currentTickets: RepoWorktreeTicketRow[],
  nextTickets: RepoWorktreeTicketRow[]
): RepoWorktreeTicketOverview {
  const total = tickets.length;
  const done = tickets.filter((ticket) => ticket.status === 'done').length;
  const open = total - done;
  const active = tickets.filter((ticket) => ticket.status === 'running' || ticket.status === 'waiting' || ticket.status === 'blocked').length;
  const failed = tickets.filter((ticket) => ticket.status === 'failed' || ticket.status === 'invalid').length;
  const previewSeen = new Set<string>();
  const preview: RepoWorktreeTicketRow[] = [];
  for (const row of [...currentTickets, ...nextTickets]) {
    if (previewSeen.has(row.id)) continue;
    previewSeen.add(row.id);
    preview.push(row);
    if (preview.length >= 3) break;
  }
  const previewIds = new Set(preview.map((row) => row.id));
  const remaining = tickets.filter(
    (ticket) => ticket.status !== 'done' && !previewIds.has(ticket.id)
  ).length;
  return { total, done, open, active, failed, preview, remaining };
}

function missingDetailViewModel(kind: RepoWorktreeKind, id: string): RepoWorktreeDetailViewModel {
  return {
    kind,
    id,
    isMissing: true,
    title: kind === 'repo' ? 'Repo not found' : 'Worktree not found',
    eyebrow: kind === 'repo' ? 'Missing repo' : 'Missing worktree',
    branch: null,
    path: null,
    stateLabel: 'Missing',
    currentRuns: [],
    flowStatus: buildTicketFlowStatusViewModel([], []),
    activity: [],
    chats: [],
    chatList: { groups: [], standaloneChats: [], totalChatCount: 0 },
    contextspace: [],
    contextspaceHref: kind === 'repo' ? '/repos' : '/worktrees',
    currentTickets: [],
    nextTickets: [],
    artifacts: [],
    links: [{ label: kind === 'repo' ? 'Back to repos' : 'Back to worktrees', href: kind === 'repo' ? '/repos' : '/worktrees', secondary: false }],
    ticketIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    ticketIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees',
    ticketOverview: { total: 0, done: 0, open: 0, active: 0, failed: 0, preview: [], remaining: 0 },
    childWorktrees: [],
    baseRepoLabel: null,
    baseRepoHref: null,
    hasActiveRun: false,
    missingIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    missingIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees',
    pmaChatHref: '/chats',
    codingAgentChatHref: '/chats',
    gitStatus: null,
    hasCarState: false,
    unboundManagedThreadCount: 0,
    chatBound: false,
    cleanupBlockedByChatBinding: false
  };
}

export function rowRelativeTime(row: { lastActivityAt?: string | null; updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.lastActivityAt ?? row.updatedAt ?? row.createdAt ?? null, now);
}

function hubTicketListLoaded(source: RepoWorktreeSourceData): boolean {
  return source.ticketsListLoaded !== false;
}

/** Single source for repo/worktree index ticket chips: `/hub/tickets` summaries scoped per row. */
function ticketIndexRollup(scoped: TicketSummary[]): { open: number; total: number; done: number } {
  const total = scoped.length;
  const done = scoped.filter((ticket) => ticket.status === 'done').length;
  const open = scoped.filter((ticket) => ticket.status !== 'done').length;
  return { open, total, done };
}

function repoToIndexRow(repo: RepoSummary, worktrees: WorktreeSummary[], source: RepoWorktreeSourceData): RepoWorktreeIndexRow {
  const listLoaded = hubTicketListLoaded(source);
  const childWorktrees = worktrees
    .map((worktree) => worktreeToNavChildRow(worktree, repo.name, source))
    .sort(byChildActiveThenLabel);
  const repoScoped = ticketsForResource(source.tickets, 'repo', repo.id);
  const rollup = listLoaded ? ticketIndexRollup(repoScoped) : { open: 0, total: 0, done: 0 };
  let dirtyWorktrees = 0;
  let inUseWorktrees = 0;
  for (const worktree of worktrees) {
    const dirty = worktree.gitStatus?.dirty === true;
    if (dirty) dirtyWorktrees += 1;
    let inUse = dirty || (worktree.activeRuns ?? 0) > 0 || worktree.status === 'running';
    if (!inUse) {
      const sig = scopedSignals(source, 'worktree', worktree.id);
      inUse = sig.active > 0 || sig.waiting > 0 || sig.failed > 0;
    }
    if (inUse) inUseWorktrees += 1;
  }
  return {
    id: repo.id,
    kind: 'repo',
    label: repo.name,
    detail: childWorktrees.length > 0
      ? `${childWorktrees.length} worktree${childWorktrees.length === 1 ? '' : 's'}`
      : null,
    status: repo.status,
    branch: repo.defaultBranch,
    path: repo.path,
    activeRuns: repo.activeRuns,
    openTickets: rollup.open,
    totalTickets: rollup.total,
    doneTickets: rollup.done,
    lastActivityAt: repo.lastActivityAt,
    href: repoRoute(repo.id),
    ticketHref: repoTicketRoute(repo.id),
    repoHref: null,
    childWorktrees,
    signalWaiting: 0,
    signalFailed: 0,
    signalActive: 0,
    pmaChatHref: scopedChatHref('repo', repo.id, 'pma'),
    codingAgentChatHref: scopedChatHref('repo', repo.id, 'agent'),
    hasCarState: boolFromRaw(repo.raw, 'has_car_state'),
    unboundManagedThreadCount: numberFromRaw(repo.raw, 'unbound_managed_thread_count'),
    chatBound: boolFromRaw(repo.raw, 'chat_bound'),
    cleanupBlockedByChatBinding: boolFromRaw(repo.raw, 'cleanup_blocked_by_chat_binding'),
    totalWorktrees: childWorktrees.length,
    inUseWorktrees,
    dirtyWorktrees
  };
}

function worktreeToNavChildRow(
  worktree: WorktreeSummary,
  repoName: string | null = null,
  source: RepoWorktreeSourceData | null = null
): RepoWorktreeChildRow {
  const listLoaded = source ? hubTicketListLoaded(source) : false;
  const scoped = source ? ticketsForResource(source.tickets, 'worktree', worktree.id) : [];
  const rollup = listLoaded ? ticketIndexRollup(scoped) : { open: 0, total: 0, done: 0 };
  return {
    id: worktree.id,
    label: shortenWorktreeLabel(worktree.name, repoName),
    status: 'idle',
    branch: worktree.branch,
    path: worktree.path,
    activeRuns: 0,
    openTickets: rollup.open,
    totalTickets: rollup.total,
    doneTickets: rollup.done,
    currentRunTitle: null,
    currentTicketId: null,
    lastActivityAt: null,
    href: worktreeRoute(worktree.id, worktree.repoId),
    ticketHref: worktreeTicketRoute(worktree.id, worktree.repoId),
    pmaChatHref: scopedChatHref('worktree', worktree.id, 'pma'),
    codingAgentChatHref: scopedChatHref('worktree', worktree.id, 'agent'),
    signalWaiting: 0,
    signalFailed: 0,
    signalActive: 0,
    hasCarState: boolFromRaw(worktree.raw, 'has_car_state'),
    unboundManagedThreadCount: numberFromRaw(worktree.raw, 'unbound_managed_thread_count'),
    chatBound: boolFromRaw(worktree.raw, 'chat_bound'),
    cleanupBlockedByChatBinding: boolFromRaw(worktree.raw, 'cleanup_blocked_by_chat_binding')
  };
}

function worktreeToIndexRow(worktree: WorktreeSummary, source: RepoWorktreeSourceData): RepoWorktreeIndexRow {
  const listLoaded = hubTicketListLoaded(source);
  const scoped = ticketsForResource(source.tickets, 'worktree', worktree.id);
  const rollup = listLoaded ? ticketIndexRollup(scoped) : { open: 0, total: 0, done: 0 };
  return {
    id: worktree.id,
    kind: 'worktree',
    label: worktree.name,
    detail: null,
    status: worktree.status,
    branch: worktree.branch,
    path: worktree.path,
    activeRuns: worktree.activeRuns,
    openTickets: rollup.open,
    totalTickets: rollup.total,
    doneTickets: rollup.done,
    lastActivityAt: worktree.lastActivityAt,
    href: worktreeRoute(worktree.id, worktree.repoId),
    ticketHref: worktree.repoId ? worktreeTicketRoute(worktree.id, worktree.repoId) : null,
    repoHref: worktree.repoId ? repoRoute(worktree.repoId) : null,
    childWorktrees: [],
    signalWaiting: 0,
    signalFailed: 0,
    signalActive: 0,
    pmaChatHref: scopedChatHref('worktree', worktree.id, 'pma'),
    codingAgentChatHref: scopedChatHref('worktree', worktree.id, 'agent'),
    hasCarState: boolFromRaw(worktree.raw, 'has_car_state'),
    unboundManagedThreadCount: numberFromRaw(worktree.raw, 'unbound_managed_thread_count'),
    chatBound: boolFromRaw(worktree.raw, 'chat_bound'),
    cleanupBlockedByChatBinding: boolFromRaw(worktree.raw, 'cleanup_blocked_by_chat_binding'),
    totalWorktrees: 0,
    inUseWorktrees: 0,
    dirtyWorktrees: 0
  };
}

function scopedChatHref(
  kind: RepoWorktreeKind,
  id: string,
  chatKind: 'pma' | 'agent' = 'pma'
): string {
  const scope = kind === 'repo' ? `repo:${encodeURIComponent(id)}` : `worktree:${encodeURIComponent(id)}`;
  return `/chats?new=${scope}&kind=${chatKind}`;
}

function shortenWorktreeLabel(name: string, repoName: string | null): string {
  if (!repoName) return name;
  const prefix = `${repoName}--`;
  return name.startsWith(prefix) ? name.slice(prefix.length) : name;
}

function mergeRunCards(
  runs: PmaRunProgress[],
  chats: PmaChatSummary[],
  scopeKind: RepoWorktreeKind,
  scopeId: string,
  parentRepoId: string | null = null
): RepoWorktreeRunCard[] {
  const cards = new Map<string, RepoWorktreeRunCard>();
  for (const run of runs) {
    const chat = run.chatId ? chats.find((candidate) => candidate.id === run.chatId) ?? null : null;
    cards.set(`run:${run.id}`, runToCard(run, chat, scopeKind, scopeId, parentRepoId));
  }
  for (const chat of chats) {
    if ([...cards.values()].some((card) => card.chatHref === `/chats?chat=${encodeURIComponent(chat.id)}`)) continue;
    cards.set(`chat:${chat.id}`, chatToCard(chat, scopeKind, scopeId, parentRepoId));
  }
  return [...cards.values()].sort(byRunRecent);
}

function scopedTicketDetail(scopeKind: RepoWorktreeKind, scopeId: string, ticketId: string, parentRepoId: string | null = null): string {
  return scopeKind === 'repo' ? repoTicketRoute(scopeId, ticketId) : worktreeTicketRoute(scopeId, parentRepoId, ticketId);
}

function runToCard(
  run: PmaRunProgress,
  chat: PmaChatSummary | null,
  scopeKind: RepoWorktreeKind,
  scopeId: string,
  parentRepoId: string | null = null
): RepoWorktreeRunCard {
  const ticketId =
    stringFromRaw(run.raw, ['ticket_id', 'current_ticket_id', 'current_ticket', 'ticket_path', 'current_ticket_path']) ??
    [...ticketAliasesFromRun(run)][0] ??
    chat?.ticketId ??
    null;
  const title = chat?.title ?? stringFromRaw(run.raw, ['title', 'current_ticket_title', 'name']) ?? ticketId ?? run.id;
  return {
    id: run.id,
    title,
    status: run.status,
    phase: run.phase,
    agentId: chat?.agentId ?? stringFromRaw(run.raw, ['agent_id', 'agent']),
    progress: chat ? progressPercent(chat, run) : run.progressPercent,
    updatedAt: run.lastEventAt ?? chat?.updatedAt ?? null,
    ticketId,
    chatHref: run.chatId ? `/chats?chat=${encodeURIComponent(run.chatId)}` : chat ? `/chats?chat=${encodeURIComponent(chat.id)}` : null,
    ticketHref: ticketId ? scopedTicketDetail(scopeKind, scopeId, ticketId, parentRepoId) : null
  };
}

function chatToCard(chat: PmaChatSummary, scopeKind: RepoWorktreeKind, scopeId: string, parentRepoId: string | null = null): RepoWorktreeRunCard {
  return {
    id: chat.id,
    title: chat.title,
    status: chat.status,
    phase: chat.model,
    agentId: chat.agentId,
    progress: progressPercent(chat),
    updatedAt: chat.updatedAt,
    ticketId: chat.ticketId,
    chatHref: `/chats?chat=${encodeURIComponent(chat.id)}`,
    ticketHref: chat.ticketId ? scopedTicketDetail(scopeKind, scopeId, chat.ticketId, parentRepoId) : null
  };
}

function buildScopedChatList(
  chats: PmaChatSummary[],
  scopeKind: RepoWorktreeKind,
  scopeId: string
): RepoWorktreeChatList {
  const entries = buildPmaChatListEntries(chats, { groupRuns: true });
  const groups: RepoWorktreeChatRunGroup[] = [];
  const standaloneChats: RepoWorktreeChatRow[] = [];
  for (const entry of entries) {
    if (entry.kind === 'chat') {
      standaloneChats.push(chatToRow(entry.chat));
      continue;
    }
    groups.push(scopedChatRunGroupToVm(entry.group));
  }
  // Suppress lone empty group for non-ticket worktrees; keep parity with chat count.
  void scopeKind;
  void scopeId;
  return { groups, standaloneChats, totalChatCount: chats.length };
}

function scopedChatRunGroupToVm(group: PmaChatRunGroup): RepoWorktreeChatRunGroup {
  const childChats = group.chats.map(chatToRow);
  return {
    key: group.key,
    scopeKind: group.scopeKind,
    scopeLabel: group.scopeLabel,
    status: group.status,
    totalCount: group.totalCount,
    activeCount: group.activeCount,
    waitingCount: group.waitingCount,
    doneCount: group.doneCount,
    failedCount: group.failedCount,
    agents: group.agents,
    updatedAt: group.updatedAt,
    chats: childChats,
    href: childChats[0]?.href ?? '/chats'
  };
}

function chatToRow(chat: PmaChatSummary): RepoWorktreeChatRow {
  const kind = pmaChatKind(chat);
  return {
    id: chat.id,
    title: chat.title,
    status: chat.status,
    kind,
    kindLabel: pmaChatKindLabel(kind),
    agentId: chat.agentId,
    model: chat.model,
    updatedAt: chat.updatedAt,
    href: `/chats?chat=${encodeURIComponent(chat.id)}`,
    ticketId: chat.ticketId
  };
}

function ticketToRow(ticket: TicketSummary, currentTicketId: string | null = null): RepoWorktreeTicketRow {
  return {
    id: ticket.id,
    title: ticket.title,
    status: ticket.status,
    href: ticketDetailHref(ticket),
    diffStats: ticket.diffStats,
    durationLabel: formatDuration(ticket.durationSeconds),
    bodyPreview: bodyPreview(ticket),
    isCurrent: ticket.id === currentTicketId || (ticket.number !== null && String(ticket.number) === currentTicketId)
  };
}

function isActiveTicketFlowStatus(status: WorkStatus): boolean {
  return status === 'running' || status === 'waiting';
}

function formatDuration(seconds: number | null): string | null {
  if (seconds === null) return null;
  const safeSeconds = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(safeSeconds / 60);
  const remainingSeconds = safeSeconds % 60;
  return minutes ? `${minutes}m ${remainingSeconds}s` : `${remainingSeconds}s`;
}

function bodyPreview(ticket: TicketSummary): string | null {
  const rawBody = ticket.raw.body ?? ticket.raw.content ?? ticket.raw.markdown;
  if (typeof rawBody !== 'string') return null;
  const body = rawBody.replace(/\s+/g, ' ').trim();
  if (!body) return null;
  return body.length > 110 ? `${body.slice(0, 107)}...` : body;
}

function artifactToRow(artifact: SurfaceArtifact): RepoWorktreeArtifactRow {
  return {
    id: artifact.id,
    title: artifact.title,
    summary: artifact.summary ?? artifact.url ?? 'Surfaced PMA artifact.',
    kind: artifact.kind,
    href: artifact.url,
    createdAt: artifact.createdAt
  };
}

const CONTEXTSPACE_ROW_ORDER = [
  { id: 'spec', filename: 'spec.md', title: 'Spec' },
  { id: 'active_context', filename: 'active_context.md', title: 'Active context' },
  { id: 'decisions', filename: 'decisions.md', title: 'Decisions' }
];

function contextspaceRows(docs: ContextspaceDocument[], contextspaceHref: string): RepoWorktreeContextspaceRow[] {
  const byFilename = new Map(docs.map((doc) => [doc.name, doc]));
  const byKind = new Map(docs.map((doc) => [doc.kind, doc]));
  return CONTEXTSPACE_ROW_ORDER.map((entry) => {
    const doc = byFilename.get(entry.filename) ?? byKind.get(entry.id);
    const content = doc?.content.trim() ?? '';
    const expand = entry.id === 'spec' && content;
    return {
      id: entry.id,
      title: entry.title,
      filename: entry.filename,
      summary: content ? firstLine(content) : 'No context recorded',
      status: content ? 'present' : 'empty',
      updatedAt: doc?.updatedAt ?? null,
      href: `${contextspaceHref}#${encodeURIComponent(entry.id)}`,
      preview: expand ? content : null,
      previewHtml: expand ? renderMarkdownToHtml(content) : null
    };
  });
}

function firstLine(content: string): string {
  const line = content
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  if (!line) return 'Context recorded';
  const normalized = line.replace(/^#+\s*/, '');
  return normalized.length > 100 ? `${normalized.slice(0, 97)}...` : normalized;
}

function runToActivity(run: RepoWorktreeRunCard): RepoWorktreeArtifactRow {
  return {
    id: `run:${run.id}`,
    title: run.title,
    summary: `${statusLabel(run.status)}${run.phase ? ` · ${run.phase}` : ''}`,
    kind: 'progress',
    href: run.chatHref ?? run.ticketHref,
    createdAt: run.updatedAt
  };
}

function ticketsForIds(tickets: TicketSummary[], ids: Set<string>, currentTicketId: string | null): RepoWorktreeTicketRow[] {
  return [...ids]
    .map((id) => {
      const aliases = new Set([id.toLowerCase()]);
      return tickets.find((ticket) => ticket.id === id || aliasesOverlap(ticketAliases(ticket), aliases)) ?? fallbackTicketSummary(id);
    })
    .map((ticket) => ticketToRow(ticket, currentTicketId));
}

function ticketsForResource(tickets: TicketSummary[], kind: RepoWorktreeKind, id: string): TicketSummary[] {
  return tickets.filter((ticket) => ticketMatchesResource(ticket, kind, id));
}

function ticketMatchesResource(ticket: TicketSummary, kind: RepoWorktreeKind, id: string): boolean {
  if (ticket.workspaceKind === kind && ticket.workspaceId === id) return true;
  if (ticket.workspaceKind === kind && ticket.workspaceId != null && ticket.workspaceId !== id) return false;
  if (kind === 'repo' && (ticket.workspaceKind === 'worktree' || ticket.worktreeId)) return false;
  const raw = ticket.raw;
  const frontmatter = asRecord(raw.frontmatter);
  const repoAliases = [
    ticket.repoId,
    stringFromRaw(raw, ['repo_id', 'base_repo_id']),
    stringFromRaw(frontmatter, ['repo_id', 'base_repo_id'])
  ];
  const worktreeAliases = [
    ticket.worktreeId,
    stringFromRaw(raw, ['worktree_id', 'worktree_repo_id']),
    stringFromRaw(frontmatter, ['worktree_id', 'worktree_repo_id'])
  ];
  const rawResourceKind = stringFromRaw(raw, ['resource_kind']);
  const frontmatterResourceKind = stringFromRaw(frontmatter, ['resource_kind']);
  const rawResourceId = stringFromRaw(raw, ['resource_id']);
  const frontmatterResourceId = stringFromRaw(frontmatter, ['resource_id']);
  return kind === 'repo'
    ? repoAliases.some((value) => value === id) || (rawResourceKind === 'repo' && rawResourceId === id) || (frontmatterResourceKind === 'repo' && frontmatterResourceId === id)
    : worktreeAliases.some((value) => value === id) ||
        (rawResourceKind === 'worktree' && rawResourceId === id) ||
        (frontmatterResourceKind === 'worktree' && frontmatterResourceId === id);
}

function fallbackTicketSummary(id: string): TicketSummary {
  return {
    id,
    number: null,
    title: id,
    status: 'running',
    workspaceKind: 'unscoped',
    workspaceId: null,
    workspacePath: null,
    repoId: null,
    worktreeId: null,
    path: null,
    ticketPath: null,
    agentId: null,
    chatKey: null,
    runId: null,
    updatedAt: null,
    durationSeconds: null,
    diffStats: null,
    errors: [],
    raw: {}
  };
}

function buildContextLinks(_kind: RepoWorktreeKind, _id: string, artifacts: RepoWorktreeArtifactRow[], _parentRepoId: string | null = null): RepoWorktreeLink[] {
  const preview = artifacts.find((artifact) => artifact.kind === 'preview_url' && artifact.href);
  return [
    ...(preview?.href ? [{ label: 'Open preview', href: preview.href, secondary: false }] : [])
  ];
}

function scopedTicketHref(kind: RepoWorktreeKind, id: string, parentRepoId: string | null = null): string {
  return kind === 'repo' ? repoTicketRoute(id) : worktreeTicketRoute(id, parentRepoId);
}

function ticketDetailHref(ticket: TicketSummary): string {
  const base =
    ticket.workspaceKind === 'repo' && ticket.workspaceId
      ? repoTicketRoute(ticket.workspaceId)
      : ticket.workspaceKind === 'worktree' && ticket.workspaceId
        ? worktreeTicketRoute(ticket.workspaceId, ticket.repoId)
        : '/chats';
  return base === '/chats' ? base : `${base}/${encodeURIComponent(ticket.number ? String(ticket.number) : ticket.id)}`;
}

function runMatchesResource(run: PmaRunProgress, kind: RepoWorktreeKind, id: string): boolean {
  const state = asRecord(run.raw.state);
  const ticketEngine = asRecord(state.ticket_engine);
  const resourceKind = stringFromRaw(run.raw, ['resource_kind']) ?? stringFromRaw(state, ['resource_kind']) ?? stringFromRaw(ticketEngine, ['resource_kind']);
  const resourceId = stringFromRaw(run.raw, ['resource_id']) ?? stringFromRaw(state, ['resource_id']) ?? stringFromRaw(ticketEngine, ['resource_id']);
  const explicitWorktreeId =
    stringFromRaw(run.raw, ['worktree_id', 'worktree_repo_id']) ??
    stringFromRaw(state, ['worktree_id', 'worktree_repo_id']) ??
    stringFromRaw(ticketEngine, ['worktree_id', 'worktree_repo_id']);
  if (kind === 'repo' && (resourceKind === 'worktree' || explicitWorktreeId)) return false;
  const keys = kind === 'repo' ? ['repo_id'] : ['worktree_id', 'worktree_repo_id'];
  return (
    keys.some((key) => run.raw[key] === id || state[key] === id || ticketEngine[key] === id) ||
    (resourceKind === kind && resourceId === id)
  );
}

function chatMatchesResource(chat: PmaChatSummary, kind: RepoWorktreeKind, id: string): boolean {
  return kind === 'repo' ? chat.repoId === id && !chat.worktreeId : chat.worktreeId === id;
}

function stringFromRaw(raw: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = raw[key] ?? asRecord(raw.state)[key] ?? asRecord(asRecord(raw.state).ticket_engine)[key];
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return null;
}

function boolFromRaw(raw: Record<string, unknown>, key: string): boolean {
  return raw[key] === true;
}

function numberFromRaw(raw: Record<string, unknown>, key: string): number {
  const value = raw[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : [];
}

function indexRowSignalPriority(row: RepoWorktreeIndexRow): number {
  let priority = 0;
  if (row.signalFailed > 0) priority += 8;
  if (row.signalWaiting > 0) priority += 4;
  if (row.signalActive > 0) priority += 2;
  return priority;
}

function bySignalsThenActiveThenRecent(left: RepoWorktreeIndexRow, right: RepoWorktreeIndexRow): number {
  const leftP = indexRowSignalPriority(left);
  const rightP = indexRowSignalPriority(right);
  if (leftP !== rightP) return rightP - leftP;
  return byActiveThenRecent(left, right);
}

function enrichIndexRowSignals(row: RepoWorktreeIndexRow, source: RepoWorktreeSourceData): RepoWorktreeIndexRow {
  const signals = scopedSignals(source, row.kind, row.id, row.childWorktrees.map((child) => child.id));
  return {
    ...row,
    signalWaiting: signals.waiting,
    signalFailed: signals.failed,
    signalActive: signals.active
  };
}

function scopedSignals(
  source: RepoWorktreeSourceData,
  kind: RepoWorktreeKind,
  id: string,
  childIds: string[] = []
): { waiting: number; failed: number; active: number } {
  const scopedChats = source.chats.filter((chat) =>
    kind === 'repo'
      ? Boolean(chat.repoId === id && !chat.worktreeId)
      : chat.worktreeId === id
  );
  const scopedRuns = source.runs.filter((run) =>
    kind === 'repo'
      ? runMatchesResource(run, 'repo', id) &&
        !childIds.some((wid) => runMatchesResource(run, 'worktree', wid))
      : runMatchesResource(run, 'worktree', id)
  );
  const chatIds = new Set(scopedChats.map((chat) => chat.id));
  let waiting = 0;
  let failed = 0;
  let active = 0;
  const bumpStatus = (status: WorkStatus) => {
    if (status === 'waiting' || status === 'blocked') waiting += 1;
    else if (status === 'failed') failed += 1;
    else if (status === 'running') active += 1;
  };
  for (const chat of scopedChats) bumpStatus(chat.status);
  for (const run of scopedRuns) {
    if (run.chatId && chatIds.has(run.chatId)) continue;
    bumpStatus(run.status);
  }
  return { waiting, failed, active };
}

function byActiveThenRecent(left: RepoWorktreeIndexRow, right: RepoWorktreeIndexRow): number {
  const leftActive = left.activeRuns > 0 || left.status === 'running' ? 1 : 0;
  const rightActive = right.activeRuns > 0 || right.status === 'running' ? 1 : 0;
  if (leftActive !== rightActive) return rightActive - leftActive;
  const leftTime = Date.parse(left.lastActivityAt ?? '') || 0;
  const rightTime = Date.parse(right.lastActivityAt ?? '') || 0;
  if (leftTime !== rightTime) return rightTime - leftTime;
  return left.label.localeCompare(right.label);
}

function byRunRecent(left: RepoWorktreeRunCard, right: RepoWorktreeRunCard): number {
  const leftTime = Date.parse(left.updatedAt ?? '') || 0;
  const rightTime = Date.parse(right.updatedAt ?? '') || 0;
  if (leftTime !== rightTime) return rightTime - leftTime;
  return left.title.localeCompare(right.title);
}

function byChildActiveThenLabel(left: RepoWorktreeChildRow, right: RepoWorktreeChildRow): number {
  const leftActive = left.activeRuns > 0 || left.status === 'running' ? 1 : 0;
  const rightActive = right.activeRuns > 0 || right.status === 'running' ? 1 : 0;
  if (leftActive !== rightActive) return rightActive - leftActive;
  const leftTime = Date.parse(left.lastActivityAt ?? '') || 0;
  const rightTime = Date.parse(right.lastActivityAt ?? '') || 0;
  if (leftTime !== rightTime) return rightTime - leftTime;
  return left.label.localeCompare(right.label);
}

function rowMatchesNeedle(row: RepoWorktreeIndexRow, needle: string): boolean {
  if (!needle) return true;
  return [row.label, row.branch ?? '', row.path ?? ''].some((value) => value.toLowerCase().includes(needle));
}

function childMatchesNeedle(child: RepoWorktreeChildRow, needle: string): boolean {
  if (!needle) return true;
  return [child.label, child.branch ?? '', child.path ?? ''].some((value) => value.toLowerCase().includes(needle));
}

function rowMatchesFilter(row: RepoWorktreeIndexRow, filter: RepoWorktreeIndexFilter): boolean {
  if (filter === 'active') return row.activeRuns > 0 || row.status === 'running' || row.signalActive > 0;
  if (filter === 'waiting') return row.status === 'waiting' || row.status === 'blocked' || row.signalWaiting > 0;
  return true;
}

function childMatchesFilter(child: RepoWorktreeChildRow, filter: RepoWorktreeIndexFilter): boolean {
  if (filter === 'active') return child.activeRuns > 0 || child.status === 'running' || child.signalActive > 0;
  if (filter === 'waiting') return child.status === 'waiting' || child.status === 'blocked' || child.signalWaiting > 0;
  return true;
}
