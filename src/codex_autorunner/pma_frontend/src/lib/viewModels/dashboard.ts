import type {
  DashboardSummary,
  PmaChatSummary,
  PmaRunProgress,
  RepoSummary,
  SensitiveApprovalRequest,
  SurfaceArtifact,
  TicketSummary,
  WorkStatus,
  WorktreeSummary
} from './domain';
import { formatRelativeTime } from './pmaChat';
import { buildTicketFlowStatusViewModel, type TicketFlowStatusViewModel } from './ticketFlowStatus';

export type DashboardMetric = {
  label: string;
  value: number;
  href: string;
  tone: 'neutral' | 'active' | 'waiting' | 'danger';
};

export type DashboardRunRow = {
  id: string;
  title: string;
  status: WorkStatus;
  /** Real progress percent if the source reports one; null means "unknown / use indeterminate motion". */
  progressPercent: number | null;
  phase: string | null;
  /** Human label like "PMA chat" / "PMA run" — distinguishes the row's source. */
  kindLabel: 'PMA chat' | 'PMA run';
  /** "2m elapsed" when the source reports duration; null otherwise. */
  elapsedLabel: string | null;
  updatedAt: string | null;
  repoId: string | null;
  worktreeId: string | null;
  ticketId: string | null;
  chatId: string | null;
  primaryHref: string;
  repoHref: string | null;
  worktreeHref: string | null;
  ticketHref: string | null;
  chatHref: string | null;
};

export type DashboardAttentionRow = {
  id: string;
  title: string;
  description: string;
  status: WorkStatus;
  kind: 'approval' | 'gate' | 'blocker' | 'unclear' | 'failure';
  updatedAt: string | null;
  primaryHref: string;
};

export type DashboardActivityRow = {
  id: string;
  title: string;
  summary: string;
  createdAt: string | null;
  href: string;
  artifact: SurfaceArtifact | null;
};

export type DashboardQueueRow = {
  id: string;
  kind: 'repo' | 'worktree';
  label: string;
  href: string;
  flowStatus: TicketFlowStatusViewModel;
  doneCount: number;
  totalCount: number;
  hasOpen: boolean;
};

export type DashboardViewModel = {
  metrics: DashboardMetric[];
  activeRuns: DashboardRunRow[];
  waitingForMe: DashboardAttentionRow[];
  failedOrBlocked: DashboardAttentionRow[];
  queues: DashboardQueueRow[];
  recentActivity: DashboardActivityRow[];
  hasAnyData: boolean;
};

export type DashboardSourceData = {
  summary: DashboardSummary | null;
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  approvals: SensitiveApprovalRequest[];
  tickets: TicketSummary[];
  repos?: RepoSummary[];
  worktrees?: WorktreeSummary[];
};

export function buildDashboardViewModel(source: DashboardSourceData): DashboardViewModel {
  const openTickets = source.tickets.filter((ticket) => ticket.status !== 'done');
  const runRows = mergeRunRows(source.runs, source.chats);
  const activeRuns = runRows.filter((row) => row.status === 'running');
  const waitingRows = [
    ...source.approvals.map(approvalToAttentionRow),
    ...runRows.filter((row) => row.status === 'waiting').map(runToAttentionRow),
    ...openTickets.filter((ticket) => ticket.status === 'waiting').map(ticketToAttentionRow)
  ];
  const failedRows = [
    ...runRows.filter((row) => row.status === 'failed' || row.status === 'blocked').map(runToFailureRow),
    ...openTickets
      .filter((ticket) => ticket.status === 'failed' || ticket.status === 'blocked')
      .map(ticketToFailureRow)
  ];
  const recentActivity = buildRecentActivity(source, runRows, openTickets);
  const queues = buildQueueRows(source);

  return {
    metrics: [
      { label: 'Active runs', value: activeRuns.length, href: '#active-runs', tone: 'active' },
      { label: 'Waiting for me', value: waitingRows.length, href: '#waiting-for-me', tone: 'waiting' },
      { label: 'Failed/blocked', value: failedRows.length, href: '#failed-blocked', tone: 'danger' },
      { label: 'Open tickets', value: openTickets.length || source.summary?.openTickets || 0, href: '#queues', tone: 'neutral' },
    ],
    activeRuns,
    waitingForMe: dedupeAttention(waitingRows).slice(0, 8),
    failedOrBlocked: dedupeAttention(failedRows).slice(0, 8),
    queues,
    recentActivity: recentActivity.slice(0, 8),
    hasAnyData:
      runRows.length > 0 ||
      source.chats.length > 0 ||
      source.approvals.length > 0 ||
      source.tickets.length > 0 ||
      (source.summary?.recentArtifacts.length ?? 0) > 0
  };
}

export function dashboardRowMeta(row: { updatedAt: string | null }, now = new Date()): string {
  return formatRelativeTime(row.updatedAt, now);
}

function mergeRunRows(runs: PmaRunProgress[], chats: PmaChatSummary[]): DashboardRunRow[] {
  const rows = new Map<string, DashboardRunRow>();
  for (const run of runs) {
    const chat = run.chatId ? chats.find((candidate) => candidate.id === run.chatId) ?? null : null;
    rows.set(`run:${run.id}`, runProgressToRow(run, chat));
  }
  for (const chat of chats) {
    if (!['running', 'waiting', 'failed', 'blocked'].includes(chat.status)) continue;
    const matchingRun = [...rows.values()].find((row) => row.chatId === chat.id);
    if (matchingRun) continue;
    rows.set(`chat:${chat.id}`, chatToRunRow(chat));
  }
  return [...rows.values()].sort(byRecentThenTitle);
}

function runProgressToRow(run: PmaRunProgress, chat: PmaChatSummary | null): DashboardRunRow {
  const ticketId = stringFromRaw(run.raw, ['ticket_id', 'current_ticket_id']) ?? chat?.ticketId ?? null;
  const repoId = stringFromRaw(run.raw, ['repo_id', 'resource_id']) ?? chat?.repoId ?? null;
  const worktreeId = stringFromRaw(run.raw, ['worktree_id', 'worktree_repo_id']) ?? chat?.worktreeId ?? null;
  const chatId = run.chatId ?? chat?.id ?? null;
  return {
    id: run.id,
    title: chat?.title ?? stringFromRaw(run.raw, ['title', 'name', 'current_ticket_title']) ?? run.id,
    status: run.status,
    progressPercent:
      typeof chat?.progressPercent === 'number'
        ? clampPercent(chat.progressPercent)
        : run.status === 'done' || run.status === 'failed'
          ? 100
          : null,
    phase: run.phase,
    kindLabel: 'PMA run',
    elapsedLabel: formatElapsedSeconds(run.elapsedSeconds),
    updatedAt: run.lastEventAt ?? chat?.updatedAt ?? null,
    repoId,
    worktreeId,
    ticketId,
    chatId,
    primaryHref: chatId ? `/pma?chat=${encodeURIComponent(chatId)}` : ticketId ? ticketHref(ticketId, repoId, worktreeId) : '/dashboard',
    repoHref: repoId ? repoHref(repoId) : null,
    worktreeHref: worktreeId ? worktreeHref(worktreeId) : null,
    ticketHref: ticketId ? ticketHref(ticketId, repoId, worktreeId) : null,
    chatHref: chatId ? `/pma?chat=${encodeURIComponent(chatId)}` : null
  };
}

function chatToRunRow(chat: PmaChatSummary): DashboardRunRow {
  return {
    id: chat.id,
    title: chat.title,
    status: chat.status,
    progressPercent:
      typeof chat.progressPercent === 'number'
        ? clampPercent(chat.progressPercent)
        : chat.status === 'done' || chat.status === 'failed'
          ? 100
          : null,
    phase: chat.model,
    kindLabel: 'PMA chat',
    elapsedLabel: null,
    updatedAt: chat.updatedAt,
    repoId: chat.repoId,
    worktreeId: chat.worktreeId,
    ticketId: chat.ticketId,
    chatId: chat.id,
    primaryHref: `/pma?chat=${encodeURIComponent(chat.id)}`,
    repoHref: chat.repoId ? repoHref(chat.repoId) : null,
    worktreeHref: chat.worktreeId ? worktreeHref(chat.worktreeId) : null,
    ticketHref: chat.ticketId ? ticketHref(chat.ticketId, chat.repoId, chat.worktreeId) : null,
    chatHref: `/pma?chat=${encodeURIComponent(chat.id)}`
  };
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function formatElapsedSeconds(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return null;
  const safe = Math.round(seconds);
  if (safe < 60) return `${safe}s elapsed`;
  const minutes = Math.floor(safe / 60);
  if (minutes < 60) return `${minutes}m elapsed`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m elapsed` : `${hours}h elapsed`;
}

function approvalToAttentionRow(approval: SensitiveApprovalRequest): DashboardAttentionRow {
  return {
    id: `approval:${approval.id}`,
    title: approval.title,
    description: approval.description || 'PMA is waiting on a sensitive CAR approval.',
    status: 'waiting',
    kind: 'approval',
    updatedAt: approval.createdAt,
    primaryHref: '/settings'
  };
}

function runToAttentionRow(row: DashboardRunRow): DashboardAttentionRow {
  return {
    id: `wait:${row.id}`,
    title: row.title,
    description: row.phase ? `Waiting during ${row.phase}.` : 'A run is paused or queued for user input.',
    status: 'waiting',
    kind: row.status === 'blocked' ? 'blocker' : 'gate',
    updatedAt: row.updatedAt,
    primaryHref: row.primaryHref
  };
}

function ticketToAttentionRow(ticket: TicketSummary): DashboardAttentionRow {
  return {
    id: `ticket-wait:${ticket.id}`,
    title: ticket.title,
    description: 'Ticket is waiting for clarification or a user gate.',
    status: ticket.status,
    kind: 'unclear',
    updatedAt: ticket.updatedAt,
    primaryHref: ticketHrefForTicket(ticket)
  };
}

function runToFailureRow(row: DashboardRunRow): DashboardAttentionRow {
  return {
    id: `failure:${row.id}`,
    title: row.title,
    description: row.status === 'blocked' ? 'Run is blocked.' : 'Run failed and needs diagnosis.',
    status: row.status,
    kind: row.status === 'blocked' ? 'blocker' : 'failure',
    updatedAt: row.updatedAt,
    primaryHref: row.primaryHref
  };
}

function ticketToFailureRow(ticket: TicketSummary): DashboardAttentionRow {
  return {
    id: `ticket-failure:${ticket.id}`,
    title: ticket.title,
    description: ticket.status === 'blocked' ? 'Ticket is blocked.' : 'Ticket run failed.',
    status: ticket.status,
    kind: ticket.status === 'blocked' ? 'blocker' : 'failure',
    updatedAt: ticket.updatedAt,
    primaryHref: ticketHrefForTicket(ticket)
  };
}

function buildQueueRows(source: DashboardSourceData): DashboardQueueRow[] {
  const repos = source.repos ?? [];
  const worktrees = source.worktrees ?? [];
  const rows: DashboardQueueRow[] = [];
  for (const repo of repos) {
    rows.push(buildQueueRow(repo.id, repo.name ?? repo.id, 'repo', source));
  }
  for (const worktree of worktrees) {
    rows.push(buildQueueRow(worktree.id, worktree.name ?? worktree.id, 'worktree', source));
  }
  return rows
    .filter((row) => row.totalCount > 0 || row.flowStatus.signal !== 'idle')
    .sort((a, b) => {
      const signalDelta = signalRank(a.flowStatus.signal) - signalRank(b.flowStatus.signal);
      if (signalDelta !== 0) return signalDelta;
      const openDelta = Number(b.hasOpen) - Number(a.hasOpen);
      if (openDelta !== 0) return openDelta;
      return a.label.localeCompare(b.label);
    });
}

function buildQueueRow(
  id: string,
  label: string,
  kind: 'repo' | 'worktree',
  source: DashboardSourceData
): DashboardQueueRow {
  const flowStatus = buildTicketFlowStatusViewModel(source.tickets, source.runs, { kind, id });
  const [doneStr, totalStr] = flowStatus.progressLabel.split('/');
  const doneCount = Number(doneStr) || 0;
  const totalCount = Number(totalStr) || 0;
  return {
    id,
    kind,
    label,
    href: `/${kind === 'repo' ? 'repos' : 'worktrees'}/${encodeURIComponent(id)}/tickets`,
    flowStatus,
    doneCount,
    totalCount,
    hasOpen: totalCount > doneCount
  };
}

function signalRank(signal: TicketFlowStatusViewModel['signal']): number {
  switch (signal) {
    case 'failed':
      return 0;
    case 'blocked':
      return 1;
    case 'waiting':
      return 2;
    case 'active':
      return 3;
    case 'idle':
      return 4;
    default:
      return 5;
  }
}

function buildRecentActivity(
  source: DashboardSourceData,
  runRows: DashboardRunRow[],
  openTickets: TicketSummary[]
): DashboardActivityRow[] {
  const artifacts = [
    ...(source.summary?.recentArtifacts ?? []),
    ...source.runs.flatMap((run) => run.events)
  ].map((artifact) => ({
    id: `artifact:${artifact.id}`,
    title: artifact.title,
    summary: artifact.summary ?? artifact.url ?? 'Surfaced PMA artifact.',
    createdAt: artifact.createdAt,
    href: artifact.url ?? '/pma',
    artifact
  }));

  const runActivity = runRows.map((row) => ({
    id: `run-activity:${row.id}`,
    title: row.title,
    summary: `${row.status}${row.phase ? ` · ${row.phase}` : ''}`,
    createdAt: row.updatedAt,
    href: row.primaryHref,
    artifact: null
  }));

  const ticketActivity = openTickets.map((ticket) => ({
    id: `ticket-activity:${ticket.id}`,
    title: ticket.title,
    summary: `Ticket ${ticket.status}`,
    createdAt: ticket.updatedAt,
    href: ticketHrefForTicket(ticket),
    artifact: null
  }));

  return [...artifacts, ...runActivity, ...ticketActivity].sort(byRecentThenTitle);
}

function dedupeAttention(rows: DashboardAttentionRow[]): DashboardAttentionRow[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const key = `${row.title}:${row.primaryHref}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function byRecentThenTitle<T extends { title?: string; label?: string; updatedAt?: string | null; createdAt?: string | null }>(
  left: T,
  right: T
): number {
  const leftTime = Date.parse(left.updatedAt ?? left.createdAt ?? '') || 0;
  const rightTime = Date.parse(right.updatedAt ?? right.createdAt ?? '') || 0;
  if (leftTime !== rightTime) return rightTime - leftTime;
  return String(left.title ?? left.label ?? '').localeCompare(String(right.title ?? right.label ?? ''));
}

function stringFromRaw(raw: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = raw[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

function repoHref(repoId: string): string {
  return `/repos/${encodeURIComponent(repoId)}`;
}

function worktreeHref(worktreeId: string): string {
  return `/worktrees/${encodeURIComponent(worktreeId)}`;
}

function ticketHref(ticketId: string, repoId: string | null, worktreeId: string | null): string {
  if (worktreeId) return `/worktrees/${encodeURIComponent(worktreeId)}/tickets/${encodeURIComponent(ticketId)}`;
  if (repoId) return `/repos/${encodeURIComponent(repoId)}/tickets/${encodeURIComponent(ticketId)}`;
  return '/dashboard';
}

function ticketHrefForTicket(ticket: TicketSummary): string {
  const routeId = ticket.number ? String(ticket.number) : ticket.id;
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return `/repos/${encodeURIComponent(ticket.workspaceId)}/tickets/${encodeURIComponent(routeId)}`;
  }
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    return `/worktrees/${encodeURIComponent(ticket.workspaceId)}/tickets/${encodeURIComponent(routeId)}`;
  }
  return ticketHref(routeId, ticket.repoId, ticket.worktreeId);
}
