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
  title: string;
  eyebrow: string;
  branch: string | null;
  path: string | null;
  stateLabel: string;
  currentRuns: RepoWorktreeRunCard[];
  activity: RepoWorktreeArtifactRow[];
  currentTickets: RepoWorktreeTicketRow[];
  nextTickets: RepoWorktreeTicketRow[];
  artifacts: RepoWorktreeArtifactRow[];
  links: RepoWorktreeLink[];
  hasActiveRun: boolean;
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
  const rows = [
    ...(kind !== 'worktree' ? source.repos.map(repoToIndexRow) : []),
    ...(kind !== 'repo' ? source.worktrees.map(worktreeToIndexRow) : [])
  ].sort(byActiveThenRecent);
  return {
    title: kind === 'worktree' ? 'Worktrees' : 'Repos',
    eyebrow: 'Workspaces',
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
  const title = resource?.name ?? id;
  const branch = kind === 'repo' ? (resource as RepoSummary | null)?.defaultBranch ?? null : (resource as WorktreeSummary | null)?.branch ?? null;
  const path = resource?.path ?? null;
  const relatedRuns = source.runs.filter((run) => runMatchesResource(run, kind, id));
  const relatedChats = source.chats.filter((chat) => chatMatchesResource(chat, kind, id));
  const runCards = mergeRunCards(relatedRuns, relatedChats);
  const activeRunCards = runCards.filter((run) => ['running', 'waiting', 'blocked'].includes(run.status));
  const visibleRuns = activeRunCards.length ? activeRunCards : runCards.slice(0, 1);
  const currentTicketIds = new Set(visibleRuns.map((run) => run.ticketId).filter((ticketId): ticketId is string => Boolean(ticketId)));
  const currentTickets = ticketsForIds(source.tickets, currentTicketIds);
  const nextTickets = source.tickets
    .filter((ticket) => ticket.status !== 'done' && !currentTicketIds.has(ticket.id))
    .slice(0, 5)
    .map(ticketToRow);
  const runArtifacts = [...source.artifacts, ...relatedRuns.flatMap((run) => run.events)].map(artifactToRow);
  const activity = [
    ...relatedRuns.flatMap((run) => run.events).map(artifactToRow),
    ...visibleRuns.map(runToActivity)
  ].slice(0, 6);

  return {
    kind,
    id,
    title,
    eyebrow: kind === 'repo' ? 'Repo current run' : 'Worktree current run',
    branch,
    path,
    stateLabel: statusLabel(resource?.status ?? visibleRuns[0]?.status ?? 'idle'),
    currentRuns: visibleRuns,
    activity,
    currentTickets,
    nextTickets,
    artifacts: runArtifacts.slice(0, 6),
    links: buildContextLinks(kind, id, runArtifacts),
    hasActiveRun: activeRunCards.length > 0
  };
}

export function rowRelativeTime(row: { lastActivityAt?: string | null; updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.lastActivityAt ?? row.updatedAt ?? row.createdAt ?? null, now);
}

function repoToIndexRow(repo: RepoSummary): RepoWorktreeIndexRow {
  return {
    id: repo.id,
    kind: 'repo',
    label: repo.name,
    detail: repo.defaultBranch ? `Default branch ${repo.defaultBranch}` : 'Repository',
    status: repo.status,
    branch: repo.defaultBranch,
    path: repo.path,
    activeRuns: repo.activeRuns,
    openTickets: repo.openTickets,
    lastActivityAt: repo.lastActivityAt,
    href: `/repos/${encodeURIComponent(repo.id)}`,
    repoHref: null
  };
}

function worktreeToIndexRow(worktree: WorktreeSummary): RepoWorktreeIndexRow {
  return {
    id: worktree.id,
    kind: 'worktree',
    label: worktree.name,
    detail: worktree.branch ? `Branch ${worktree.branch}` : 'Worktree',
    status: worktree.status,
    branch: worktree.branch,
    path: worktree.path,
    activeRuns: worktree.activeRuns,
    openTickets: worktree.openTickets,
    lastActivityAt: worktree.lastActivityAt,
    href: `/worktrees/${encodeURIComponent(worktree.id)}`,
    repoHref: worktree.repoId ? `/repos/${encodeURIComponent(worktree.repoId)}` : null
  };
}

function mergeRunCards(runs: PmaRunProgress[], chats: PmaChatSummary[]): RepoWorktreeRunCard[] {
  const cards = new Map<string, RepoWorktreeRunCard>();
  for (const run of runs) {
    const chat = run.chatId ? chats.find((candidate) => candidate.id === run.chatId) ?? null : null;
    cards.set(`run:${run.id}`, runToCard(run, chat));
  }
  for (const chat of chats) {
    if ([...cards.values()].some((card) => card.chatHref === `/pma?chat=${encodeURIComponent(chat.id)}`)) continue;
    cards.set(`chat:${chat.id}`, chatToCard(chat));
  }
  return [...cards.values()].sort(byRunRecent);
}

function runToCard(run: PmaRunProgress, chat: PmaChatSummary | null): RepoWorktreeRunCard {
  const ticketId = stringFromRaw(run.raw, ['ticket_id', 'current_ticket_id', 'current_ticket']) ?? chat?.ticketId ?? null;
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
    chatHref: run.chatId ? `/pma?chat=${encodeURIComponent(run.chatId)}` : chat ? `/pma?chat=${encodeURIComponent(chat.id)}` : null,
    ticketHref: ticketId ? `/tickets/${encodeURIComponent(ticketId)}` : null,
    logsHref: `/api/flows/${encodeURIComponent(run.id)}/dispatch_history`
  };
}

function chatToCard(chat: PmaChatSummary): RepoWorktreeRunCard {
  return {
    id: chat.id,
    title: chat.title,
    status: chat.status,
    phase: chat.model,
    agentId: chat.agentId,
    progress: progressPercent(chat),
    updatedAt: chat.updatedAt,
    ticketId: chat.ticketId,
    chatHref: `/pma?chat=${encodeURIComponent(chat.id)}`,
    ticketHref: chat.ticketId ? `/tickets/${encodeURIComponent(chat.ticketId)}` : null,
    logsHref: null
  };
}

function ticketToRow(ticket: TicketSummary): RepoWorktreeTicketRow {
  return {
    id: ticket.id,
    title: ticket.title,
    status: ticket.status,
    href: `/tickets/${encodeURIComponent(ticket.id)}`
  };
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

function ticketsForIds(tickets: TicketSummary[], ids: Set<string>): RepoWorktreeTicketRow[] {
  return [...ids]
    .map((id) => tickets.find((ticket) => ticket.id === id) ?? fallbackTicketSummary(id))
    .map(ticketToRow);
}

function fallbackTicketSummary(id: string): TicketSummary {
  return {
    id,
    number: null,
    title: id,
    status: 'running',
    repoId: null,
    worktreeId: null,
    path: null,
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
  return [
    { label: 'Open PMA chat', href: '/pma', secondary: false },
    { label: 'View tickets', href: '/tickets', secondary: false },
    { label: 'View contextspace', href: `/contextspace/${encodeURIComponent(id)}`, secondary: false },
    ...(preview?.href ? [{ label: 'Open preview', href: preview.href, secondary: false }] : []),
    { label: kind === 'repo' ? 'Debug repo logs' : 'Debug worktree logs', href: '/tickets', secondary: true }
  ];
}

function runMatchesResource(run: PmaRunProgress, kind: RepoWorktreeKind, id: string): boolean {
  const keys = kind === 'repo' ? ['repo_id', 'resource_id', 'base_repo_id'] : ['worktree_id', 'worktree_repo_id', 'repo_id', 'resource_id'];
  const state = asRecord(run.raw.state);
  const ticketEngine = asRecord(state.ticket_engine);
  return keys.some((key) => run.raw[key] === id || state[key] === id || ticketEngine[key] === id);
}

function chatMatchesResource(chat: PmaChatSummary, kind: RepoWorktreeKind, id: string): boolean {
  return kind === 'repo' ? chat.repoId === id : chat.worktreeId === id || chat.repoId === id;
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
