import type {
  PmaChatSummary,
  PmaRunProgress,
  RepoSummary,
  SurfaceArtifact,
  TicketSummary,
  WorkStatus,
  WorktreeSummary
} from './domain';
import { formatRelativeTime, progressPercent, statusLabel } from './pmaChat';
import {
  aliasesOverlap,
  buildTicketFlowStatusViewModel,
  ticketAliases,
  ticketAliasesFromRun,
  type TicketFlowStatusViewModel
} from './ticketFlowStatus';

export type RepoWorktreeKind = 'repo' | 'worktree';

export type RepoWorktreeIndexRow = {
  id: string;
  kind: RepoWorktreeKind;
  label: string;
  detail: string;
  status: WorkStatus;
  branch: string | null;
  path: string | null;
  activeRuns: number;
  openTickets: number;
  lastActivityAt: string | null;
  href: string;
  repoHref: string | null;
  childWorktrees: RepoWorktreeChildRow[];
  /** PMA chats + ticket-flow runs scoped to this row (runs tied to an already-counted chat are skipped). */
  signalWaiting: number;
  signalFailed: number;
  signalActive: number;
  /** Deep-link into chats with the new-chat scope picker preset. */
  chatNewHref: string;
};

export type RepoWorktreeChildRow = {
  id: string;
  label: string;
  status: WorkStatus;
  branch: string | null;
  path: string | null;
  activeRuns: number;
  openTickets: number;
  currentRunTitle: string | null;
  currentTicketId: string | null;
  lastActivityAt: string | null;
  href: string;
  ticketHref: string | null;
};

export type RepoWorktreeRunCard = {
  id: string;
  title: string;
  status: WorkStatus;
  phase: string | null;
  agentId: string | null;
  progress: number;
  updatedAt: string | null;
  ticketId: string | null;
  chatHref: string | null;
  ticketHref: string | null;
  logsHref: string | null;
};

export type RepoWorktreeTicketRow = {
  id: string;
  title: string;
  status: WorkStatus;
  href: string;
  diffLabel: string | null;
  durationLabel: string | null;
  bodyPreview: string | null;
  isCurrent: boolean;
};

export type RepoWorktreeArtifactRow = {
  id: string;
  title: string;
  summary: string;
  kind: SurfaceArtifact['kind'];
  href: string | null;
  createdAt: string | null;
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
};

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
  currentTickets: RepoWorktreeTicketRow[];
  nextTickets: RepoWorktreeTicketRow[];
  artifacts: RepoWorktreeArtifactRow[];
  links: RepoWorktreeLink[];
  ticketIndexHref: string;
  ticketIndexLabel: string;
  childWorktrees: RepoWorktreeChildRow[];
  baseRepoLabel: string | null;
  baseRepoHref: string | null;
  hasActiveRun: boolean;
  missingIndexHref: string;
  missingIndexLabel: string;
};

export type RepoWorktreeSourceData = {
  repos: RepoSummary[];
  worktrees: WorktreeSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  tickets: TicketSummary[];
  artifacts: SurfaceArtifact[];
};

export function buildRepoWorktreeIndexViewModel(
  source: RepoWorktreeSourceData,
  kind: 'all' | RepoWorktreeKind = 'all'
): RepoWorktreeIndexViewModel {
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
    activeCount: rows.filter((row) => row.status === 'running').length,
    waitingCount: rows.filter((row) => row.status === 'waiting' || row.status === 'blocked').length,
    openTicketCount: rows.reduce((total, row) => total + row.openTickets, 0)
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
  const relatedRuns = source.runs.filter((run) => runMatchesResource(run, kind, id));
  const relatedChats = source.chats.filter((chat) => chatMatchesResource(chat, kind, id));
  const runCards = mergeRunCards(relatedRuns, relatedChats, kind, id);
  const activeRunCards = runCards.filter((run) => ['running', 'waiting', 'blocked'].includes(run.status));
  const visibleRuns = activeRunCards.length ? activeRunCards : runCards.slice(0, 1);
  const currentTicketIds = new Set(visibleRuns.map((run) => run.ticketId).filter((ticketId): ticketId is string => Boolean(ticketId)));
  const scopedTickets = ticketsForResource(source.tickets, kind, id);
  const flowStatus = buildTicketFlowStatusViewModel(scopedTickets, relatedRuns, { kind, id });
  const currentTickets = ticketsForIds(scopedTickets, currentTicketIds, flowStatus.currentTicketId);
  const nextTickets = scopedTickets
    .filter((ticket) => ticket.status !== 'done' && !currentTicketIds.has(ticket.id))
    .slice(0, 5)
    .map((ticket) => ticketToRow(ticket, flowStatus.currentTicketId));
  const runArtifacts = [...source.artifacts, ...relatedRuns.flatMap((run) => run.events)].map(artifactToRow);
  const activity = [
    ...relatedRuns.flatMap((run) => run.events).map(artifactToRow),
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
    currentTickets,
    nextTickets,
    artifacts: runArtifacts.slice(0, 6),
    links: buildContextLinks(kind, id, runArtifacts),
    ticketIndexHref: scopedTicketHref(kind, id),
    ticketIndexLabel: kind === 'repo' ? 'Repo tickets' : 'Worktree tickets',
    childWorktrees: childWorktreeSummaries.map((worktree) => worktreeToChildRow(worktree, source)),
    baseRepoLabel: baseRepo?.name ?? (kind === 'worktree' ? (resource as WorktreeSummary | null)?.repoId ?? null : null),
    baseRepoHref: kind === 'worktree' && (resource as WorktreeSummary | null)?.repoId ? `/repos/${encodeURIComponent((resource as WorktreeSummary).repoId as string)}` : null,
    hasActiveRun: activeRunCards.length > 0,
    missingIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    missingIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees'
  };
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
    currentTickets: [],
    nextTickets: [],
    artifacts: [],
    links: [{ label: kind === 'repo' ? 'Back to repos' : 'Back to worktrees', href: kind === 'repo' ? '/repos' : '/worktrees', secondary: false }],
    ticketIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    ticketIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees',
    childWorktrees: [],
    baseRepoLabel: null,
    baseRepoHref: null,
    hasActiveRun: false,
    missingIndexHref: kind === 'repo' ? '/repos' : '/worktrees',
    missingIndexLabel: kind === 'repo' ? 'Back to repos' : 'Back to worktrees'
  };
}

export function rowRelativeTime(row: { lastActivityAt?: string | null; updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.lastActivityAt ?? row.updatedAt ?? row.createdAt ?? null, now);
}

function repoToIndexRow(repo: RepoSummary, worktrees: WorktreeSummary[], source: RepoWorktreeSourceData): RepoWorktreeIndexRow {
  const childWorktrees = worktrees.map((worktree) => worktreeToChildRow(worktree, source)).sort(byChildActiveThenLabel);
  const childActiveRuns = childWorktrees.reduce((total, worktree) => total + worktree.activeRuns, 0);
  const childOpenTickets = childWorktrees.reduce((total, worktree) => total + worktree.openTickets, 0);
  return {
    id: repo.id,
    kind: 'repo',
    label: repo.name,
    detail: repo.defaultBranch
      ? `Default branch ${repo.defaultBranch} · ${childWorktrees.length} worktree${childWorktrees.length === 1 ? '' : 's'}`
      : `Repository · ${childWorktrees.length} worktree${childWorktrees.length === 1 ? '' : 's'}`,
    status: aggregateStatus(repo.status, childWorktrees.map((worktree) => worktree.status)),
    branch: repo.defaultBranch,
    path: repo.path,
    activeRuns: repo.activeRuns + childActiveRuns,
    openTickets: repo.openTickets + childOpenTickets,
    lastActivityAt: mostRecent([repo.lastActivityAt, ...childWorktrees.map((worktree) => worktree.lastActivityAt)]),
    href: `/repos/${encodeURIComponent(repo.id)}`,
    repoHref: null,
    childWorktrees,
    signalWaiting: 0,
    signalFailed: 0,
    signalActive: 0,
    chatNewHref: `/chats?new=repo:${encodeURIComponent(repo.id)}`
  };
}

function worktreeToIndexRow(worktree: WorktreeSummary, _source: RepoWorktreeSourceData): RepoWorktreeIndexRow {
  return {
    id: worktree.id,
    kind: 'worktree',
    label: worktree.name,
    detail: worktree.branch ? `Repo worktree variant · branch ${worktree.branch}` : 'Repo worktree variant',
    status: worktree.status,
    branch: worktree.branch,
    path: worktree.path,
    activeRuns: worktree.activeRuns,
    openTickets: worktree.openTickets,
    lastActivityAt: worktree.lastActivityAt,
    href: `/worktrees/${encodeURIComponent(worktree.id)}`,
    repoHref: worktree.repoId ? `/repos/${encodeURIComponent(worktree.repoId)}` : null,
    childWorktrees: [],
    signalWaiting: 0,
    signalFailed: 0,
    signalActive: 0,
    chatNewHref: worktree.repoId
      ? `/chats?new=repo:${encodeURIComponent(worktree.repoId)}`
      : `/chats?new=worktree:${encodeURIComponent(worktree.id)}`
  };
}

function worktreeToChildRow(worktree: WorktreeSummary, source: RepoWorktreeSourceData): RepoWorktreeChildRow {
  const run = mergeRunCards(
    source.runs.filter((candidate) => runMatchesResource(candidate, 'worktree', worktree.id)),
    source.chats.filter((candidate) => chatMatchesResource(candidate, 'worktree', worktree.id)),
    'worktree',
    worktree.id
  )[0];
  const tickets = ticketsForResource(source.tickets, 'worktree', worktree.id).filter((ticket) => ticket.status !== 'done');
  const currentTicketId = run?.ticketId ?? tickets[0]?.id ?? null;
  return {
    id: worktree.id,
    label: worktree.name,
    status: run?.status ?? worktree.status,
    branch: worktree.branch,
    path: worktree.path,
    activeRuns: worktree.activeRuns || (run && ['running', 'waiting', 'blocked'].includes(run.status) ? 1 : 0),
    openTickets: worktree.openTickets || tickets.length,
    currentRunTitle: run?.title ?? null,
    currentTicketId,
    lastActivityAt: worktree.lastActivityAt,
    href: `/worktrees/${encodeURIComponent(worktree.id)}`,
    ticketHref: `/worktrees/${encodeURIComponent(worktree.id)}/tickets`
  };
}

function mergeRunCards(
  runs: PmaRunProgress[],
  chats: PmaChatSummary[],
  scopeKind: RepoWorktreeKind,
  scopeId: string
): RepoWorktreeRunCard[] {
  const cards = new Map<string, RepoWorktreeRunCard>();
  for (const run of runs) {
    const chat = run.chatId ? chats.find((candidate) => candidate.id === run.chatId) ?? null : null;
    cards.set(`run:${run.id}`, runToCard(run, chat, scopeKind, scopeId));
  }
  for (const chat of chats) {
    if ([...cards.values()].some((card) => card.chatHref === `/chats?chat=${encodeURIComponent(chat.id)}`)) continue;
    cards.set(`chat:${chat.id}`, chatToCard(chat, scopeKind, scopeId));
  }
  return [...cards.values()].sort(byRunRecent);
}

function scopedTicketDetail(scopeKind: RepoWorktreeKind, scopeId: string, ticketId: string): string {
  return `/${scopeKind === 'repo' ? 'repos' : 'worktrees'}/${encodeURIComponent(scopeId)}/tickets/${encodeURIComponent(ticketId)}`;
}

function runToCard(
  run: PmaRunProgress,
  chat: PmaChatSummary | null,
  scopeKind: RepoWorktreeKind,
  scopeId: string
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
    progress: chat ? progressPercent(chat, run) : run.status === 'running' ? 64 : run.status === 'waiting' ? 28 : run.status === 'done' ? 100 : 0,
    updatedAt: run.lastEventAt ?? chat?.updatedAt ?? null,
    ticketId,
    chatHref: run.chatId ? `/chats?chat=${encodeURIComponent(run.chatId)}` : chat ? `/chats?chat=${encodeURIComponent(chat.id)}` : null,
    ticketHref: ticketId ? scopedTicketDetail(scopeKind, scopeId, ticketId) : null,
    logsHref: `/api/flows/${encodeURIComponent(run.id)}/dispatch_history`
  };
}

function chatToCard(chat: PmaChatSummary, scopeKind: RepoWorktreeKind, scopeId: string): RepoWorktreeRunCard {
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
    ticketHref: chat.ticketId ? scopedTicketDetail(scopeKind, scopeId, chat.ticketId) : null,
    logsHref: null
  };
}

function ticketToRow(ticket: TicketSummary, currentTicketId: string | null = null): RepoWorktreeTicketRow {
  return {
    id: ticket.id,
    title: ticket.title,
    status: ticket.status,
    href: ticketDetailHref(ticket),
    diffLabel: ticketDiffLabel(ticket),
    durationLabel: formatDuration(ticket.durationSeconds),
    bodyPreview: bodyPreview(ticket),
    isCurrent: ticket.id === currentTicketId || (ticket.number !== null && String(ticket.number) === currentTicketId)
  };
}

function ticketDiffLabel(ticket: TicketSummary): string | null {
  const stats = ticket.diffStats;
  if (!stats) return null;
  const parts = [
    stats.insertions ? `+${stats.insertions}` : null,
    stats.deletions ? `-${stats.deletions}` : null,
    stats.filesChanged ? `${stats.filesChanged} files` : null
  ].filter(Boolean);
  return parts.length ? parts.join(' ') : null;
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

function buildContextLinks(kind: RepoWorktreeKind, id: string, artifacts: RepoWorktreeArtifactRow[]): RepoWorktreeLink[] {
  const preview = artifacts.find((artifact) => artifact.kind === 'preview_url' && artifact.href);
  const scopeLabel = kind === 'repo' ? 'repo' : 'worktree';
  return [
    { label: `View ${scopeLabel} tickets`, href: scopedTicketHref(kind, id), secondary: false },
    { label: `View ${scopeLabel} memory`, href: `/contextspace/${encodeURIComponent(id)}`, secondary: false },
    ...(preview?.href ? [{ label: 'Open preview', href: preview.href, secondary: false }] : [])
  ];
}

function scopedTicketHref(kind: RepoWorktreeKind, id: string): string {
  return `/${kind === 'repo' ? 'repos' : 'worktrees'}/${encodeURIComponent(id)}/tickets`;
}

function ticketDetailHref(ticket: TicketSummary): string {
  const base =
    ticket.workspaceKind === 'repo' && ticket.workspaceId
      ? `/repos/${encodeURIComponent(ticket.workspaceId)}/tickets`
      : ticket.workspaceKind === 'worktree' && ticket.workspaceId
        ? `/worktrees/${encodeURIComponent(ticket.workspaceId)}/tickets`
        : '/chats';
  return base === '/chats' ? base : `${base}/${encodeURIComponent(ticket.number ? String(ticket.number) : ticket.id)}`;
}

function runMatchesResource(run: PmaRunProgress, kind: RepoWorktreeKind, id: string): boolean {
  const keys = kind === 'repo' ? ['repo_id', 'resource_id', 'base_repo_id'] : ['worktree_id', 'worktree_repo_id'];
  const state = asRecord(run.raw.state);
  const ticketEngine = asRecord(state.ticket_engine);
  const resourceKind = stringFromRaw(run.raw, ['resource_kind']) ?? stringFromRaw(state, ['resource_kind']) ?? stringFromRaw(ticketEngine, ['resource_kind']);
  const resourceId = stringFromRaw(run.raw, ['resource_id']) ?? stringFromRaw(state, ['resource_id']) ?? stringFromRaw(ticketEngine, ['resource_id']);
  return (
    keys.some((key) => run.raw[key] === id || state[key] === id || ticketEngine[key] === id) ||
    (resourceKind === kind && resourceId === id)
  );
}

function chatMatchesResource(chat: PmaChatSummary, kind: RepoWorktreeKind, id: string): boolean {
  return kind === 'repo' ? chat.repoId === id : chat.worktreeId === id;
}

function stringFromRaw(raw: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = raw[key] ?? asRecord(raw.state)[key] ?? asRecord(asRecord(raw.state).ticket_engine)[key];
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
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
  const childIds = row.childWorktrees.map((child) => child.id);
  const scopedChats = source.chats.filter((chat) =>
    row.kind === 'repo'
      ? chat.repoId === row.id || (chat.worktreeId ? childIds.includes(chat.worktreeId) : false)
      : chat.worktreeId === row.id
  );
  const scopedRuns = source.runs.filter((run) =>
    row.kind === 'repo'
      ? runMatchesResource(run, 'repo', row.id) || childIds.some((wid) => runMatchesResource(run, 'worktree', wid))
      : runMatchesResource(run, 'worktree', row.id)
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
  return {
    ...row,
    signalWaiting: waiting,
    signalFailed: failed,
    signalActive: active
  };
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

function mostRecent(values: (string | null)[]): string | null {
  return values
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => (Date.parse(right) || 0) - (Date.parse(left) || 0))[0] ?? null;
}

function aggregateStatus(base: WorkStatus, children: WorkStatus[]): WorkStatus {
  const statuses = [base, ...children];
  if (statuses.includes('running')) return 'running';
  if (statuses.includes('blocked')) return 'blocked';
  if (statuses.includes('waiting')) return 'waiting';
  if (statuses.includes('failed')) return 'failed';
  if (statuses.includes('idle')) return 'idle';
  return base;
}
