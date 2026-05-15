import type { PmaRunProgress, TicketSummary, WorkStatus } from './domain';
import { formatRelativeTime, statusLabel } from './pmaChat';
import { repoTicketRoute, worktreeTicketRoute } from './routes';

export type TicketFlowStatusViewModel = {
  status: WorkStatus;
  statusLabel: string;
  currentTicketLabel: string;
  currentTicketHref: string | null;
  currentTicketId: string | null;
  turnsLabel: string;
  elapsedLabel: string;
  progressLabel: string;
  lastActivityLabel: string;
  reasonLabel: string;
  signal: 'active' | 'waiting' | 'blocked' | 'failed' | 'invalid' | 'idle' | 'done';
};

export type TicketFlowOwnerScope = {
  kind: 'repo' | 'worktree';
  id: string;
} | null;

export function buildTicketFlowStatusViewModel(
  tickets: TicketSummary[],
  runs: PmaRunProgress[],
  owner: TicketFlowOwnerScope = null,
  now = new Date()
): TicketFlowStatusViewModel {
  const scopedTickets = owner ? tickets.filter((ticket) => ticketMatchesOwner(ticket, owner)) : tickets;
  const scopedRuns = owner ? runs.filter((run) => runMatchesOwner(run, owner)) : runs;
  const activeRun = selectPrimaryRun(scopedRuns);
  const recentRun = activeRun ?? mostRecentRun(scopedRuns);
  const run = activeRun ?? recentRun;
  const currentTicket = run ? findTicketForRun(scopedTickets, run) : scopedTickets.find((ticket) => ticket.status !== 'done') ?? null;
  const doneCount = scopedTickets.filter((ticket) => ticket.status === 'done').length;
  const totalCount = scopedTickets.length;
  const status = activeRun?.status ?? currentTicket?.status ?? (doneCount > 0 && doneCount === totalCount ? 'done' : 'idle');
  const recoveryState = recoveryStateFromRun(run);
  const lastActivityAt =
    run?.lastEventAt ??
    currentTicket?.updatedAt ??
    mostRecent(scopedTickets.map((ticket) => ticket.updatedAt)) ??
    mostRecent(scopedRuns.flatMap((entry) => [entry.lastEventAt, dateFromRaw(entry.raw, ['finished_at', 'started_at', 'created_at'])]));
  const turns = numberFromRaw(run?.raw, [
    'turn_count',
    'turns',
    'iteration',
    'iteration_count',
    'state.turn_count',
    'state.ticket_engine.ticket_turns',
    'state.ticket_engine.total_turns',
    'ticket_engine.ticket_turns',
    'ticket_engine.total_turns'
  ]);
  const elapsed =
    run?.elapsedSeconds ??
    numberFromRaw(run?.raw, ['elapsed_seconds', 'duration_seconds', 'state.elapsed_seconds']);
  return {
    status,
    statusLabel: recoveryStatusLabel(recoveryState) ?? statusLabel(status),
    currentTicketLabel: currentTicket ? ticketDisplayLabel(currentTicket) : 'None',
    currentTicketHref: currentTicket ? ticketDetailHref(currentTicket) : null,
    currentTicketId: currentTicket?.id ?? null,
    turnsLabel: formatCount(turns),
    elapsedLabel: formatElapsed(elapsed),
    progressLabel: `${doneCount}/${totalCount}`,
    lastActivityLabel: formatRelativeTime(lastActivityAt, now),
    reasonLabel: recoveryReasonFromRun(run) ?? reasonFromRun(run) ?? reasonFromTickets(scopedTickets) ?? 'No reason reported',
    signal: recoverySignal(recoveryState) ?? statusSignal(status)
  };
}

export function ticketAliases(ticket: TicketSummary): Set<string> {
  return new Set(
    [
      ticket.id,
      ticket.path,
      ticket.ticketPath,
      ticket.chatKey,
      ticket.runId,
      ticket.number ? `TICKET-${String(ticket.number).padStart(3, '0')}.md` : null,
      ticket.number ? `TICKET-${String(ticket.number).padStart(3, '0')}` : null,
      ticket.number ? String(ticket.number) : null
    ]
      .filter((value): value is string => Boolean(value))
      .flatMap(aliasVariants)
  );
}

export function ticketAliasesFromRun(run: PmaRunProgress): Set<string> {
  return new Set(
    [
      'ticket_id',
      'current_ticket_id',
      'current_ticket',
      'ticket_path',
      'current_ticket_path',
      'ticket_engine.ticket_id',
      'ticket_engine.current_ticket',
      'ticket_engine.current_ticket_path',
      'state.ticket_id',
      'state.current_ticket_id',
      'state.current_ticket',
      'state.ticket_path',
      'state.current_ticket_path',
      'state.ticket_engine.ticket_id',
      'state.ticket_engine.current_ticket_id',
      'state.ticket_engine.current_ticket',
      'state.ticket_engine.current_ticket_path'
    ]
      .map((key) => stringFromRaw(run.raw, key))
      .filter((value): value is string => Boolean(value))
      .flatMap(aliasVariants)
  );
}

export function aliasesOverlap(left: Set<string>, right: Set<string>): boolean {
  for (const alias of left) {
    if (right.has(alias)) return true;
  }
  return false;
}

function selectPrimaryRun(runs: PmaRunProgress[]): PmaRunProgress | null {
  const actionableRuns = runs.filter((run) => !isPendingStopRequestedRun(run));
  return (
    actionableRuns.find((run) => run.status === 'running') ??
    actionableRuns.find((run) => run.status === 'waiting' || run.status === 'blocked' || run.status === 'failed') ??
    null
  );
}

function isPendingStopRequestedRun(run: PmaRunProgress): boolean {
  const rawStatus = stringFromRaw(run.raw, 'status') ?? run.status;
  const stopRequested = rawValue(run.raw, 'stop_requested');
  return String(rawStatus).trim().toLowerCase() === 'pending' && isTruthyRaw(stopRequested);
}

function isTruthyRaw(value: unknown): boolean {
  if (value === true || value === 1) return true;
  if (typeof value === 'string') return ['1', 'true', 'yes'].includes(value.trim().toLowerCase());
  return false;
}

function mostRecentRun(runs: PmaRunProgress[]): PmaRunProgress | null {
  return (
    [...runs].sort(
      (left, right) =>
        (runRecencyTimestamp(right) || 0) - (runRecencyTimestamp(left) || 0)
    )[0] ?? null
  );
}

function runRecencyTimestamp(run: PmaRunProgress): number {
  const candidates = [
    run.lastEventAt,
    dateFromRaw(run.raw, ['finished_at', 'started_at', 'created_at'])
  ];
  for (const value of candidates) {
    if (!value) continue;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function dateFromRaw(raw: Record<string, unknown> | undefined, keys: string[]): string | null {
  if (!raw) return null;
  for (const key of keys) {
    const value = rawValue(raw, key);
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

function findTicketForRun(tickets: TicketSummary[], run: PmaRunProgress): TicketSummary | null {
  const runAliases = ticketAliasesFromRun(run);
  return tickets.find((ticket) => aliasesOverlap(ticketAliases(ticket), runAliases)) ?? null;
}

function ticketMatchesOwner(ticket: TicketSummary, owner: Exclude<TicketFlowOwnerScope, null>): boolean {
  if (ticket.workspaceKind === owner.kind && ticket.workspaceId === owner.id) return true;
  if (owner.kind === 'repo') {
    if (ticket.workspaceKind === 'worktree' || ticket.worktreeId) return false;
    return ticket.repoId === owner.id;
  }
  return ticket.worktreeId === owner.id;
}

function runMatchesOwner(run: PmaRunProgress, owner: Exclude<TicketFlowOwnerScope, null>): boolean {
  const resourceKind = stringFromRaw(run.raw, 'resource_kind') ?? stringFromRaw(run.raw, 'state.resource_kind') ?? stringFromRaw(run.raw, 'state.ticket_engine.resource_kind');
  const resourceId = stringFromRaw(run.raw, 'resource_id') ?? stringFromRaw(run.raw, 'state.resource_id') ?? stringFromRaw(run.raw, 'state.ticket_engine.resource_id');
  if (resourceKind === owner.kind && resourceId === owner.id) return true;
  const worktreeKeys = [
    'worktree_id',
    'worktree_repo_id',
    'state.worktree_id',
    'state.worktree_repo_id',
    'input_data.worktree_id',
    'input_data.worktree_repo_id',
    'state.ticket_engine.worktree_id'
  ];
  const explicitWorktreeId = worktreeKeys.map((key) => stringFromRaw(run.raw, key)).find((v): v is string => Boolean(v));
  if (owner.kind === 'repo' && (resourceKind === 'worktree' || explicitWorktreeId)) return false;
  const keys =
    owner.kind === 'repo'
      ? ['repo_id', 'state.repo_id', 'input_data.repo_id']
      : ['worktree_id', 'worktree_repo_id', 'state.worktree_id', 'state.worktree_repo_id', 'input_data.worktree_id', 'input_data.worktree_repo_id'];
  if (keys.some((key) => stringFromRaw(run.raw, key) === owner.id)) return true;
  return false;
}

function ticketDisplayLabel(ticket: TicketSummary): string {
  const number = ticket.number ? `#${ticket.number}` : ticket.path?.split('/').pop()?.replace(/\.md$/, '') ?? ticket.id;
  return `${number} ${ticket.title}`;
}

function ticketDetailHref(ticket: TicketSummary): string {
  const routeId = ticket.number ? String(ticket.number) : ticket.id;
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return repoTicketRoute(ticket.workspaceId, routeId);
  }
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    return worktreeTicketRoute(ticket.workspaceId, ticket.repoId, routeId);
  }
  return '/chats';
}

function reasonFromRun(run: PmaRunProgress | null): string | null {
  if (!run) return null;
  return (
    run.guidance ??
    stringFromRaw(run.raw, 'reason') ??
    stringFromRaw(run.raw, 'waiting_reason') ??
    stringFromRaw(run.raw, 'blocked_reason') ??
    stringFromRaw(run.raw, 'failure_reason') ??
    stringFromRaw(run.raw, 'state.reason') ??
    stringFromRaw(run.raw, 'state.ticket_engine.reason') ??
    run.phase
  );
}

function recoveryStateFromRun(run: PmaRunProgress | null): string | null {
  if (!run) return null;
  return (
    stringFromRaw(run.raw, 'run_state.recovery_projection.primary_state') ??
    stringFromRaw(run.raw, 'run_state.recovery_state') ??
    stringFromRaw(run.raw, 'canonical_state_v1.recovery_projection.primary_state') ??
    stringFromRaw(run.raw, 'canonical_state_v1.recovery_state') ??
    stringFromRaw(run.raw, 'recovery_state')
  );
}

function recoveryReasonFromRun(run: PmaRunProgress | null): string | null {
  if (!run) return null;
  const state = recoveryStateFromRun(run);
  if (state === 'commit_barrier_pending' || state === 'commit_barrier_exhausted') {
    return (
      stringFromRaw(run.raw, 'run_state.recovery_projection.facets.commit_barrier.reason') ??
      (state === 'commit_barrier_exhausted'
        ? 'Commit barrier retry budget exhausted; resolve the worktree before resuming'
        : 'Committing or preserving completed ticket work before advancing')
    );
  }
  if (state === 'restart_exhausted') {
    return (
      stringFromRaw(run.raw, 'run_state.blocking_reason') ??
      'Restart attempts exhausted; inspect the crash artifact and resume or restart intentionally'
    );
  }
  if (state === 'stale_alive') {
    return (
      stringFromRaw(run.raw, 'run_state.blocking_reason') ??
      stringFromRaw(run.raw, 'canonical_state_v1.stale_reason') ??
      'Worker is alive but semantic progress is stale'
    );
  }
  if (state === 'recovering') {
    return stringFromRaw(run.raw, 'run_state.crash_reason') ?? 'Worker recovery in progress';
  }
  if (state === 'restarted') {
    return 'Worker restarted after recovery';
  }
  return null;
}

function recoveryStatusLabel(state: string | null): string | null {
  if (state === 'commit_barrier_pending') return 'Preserving work';
  if (state === 'commit_barrier_exhausted') return 'Commit blocked';
  if (state === 'restart_exhausted') return 'Recovery exhausted';
  if (state === 'stale_alive') return 'Needs attention';
  if (state === 'recovering') return 'Recovering';
  if (state === 'restarted') return 'Restarted';
  if (state === 'failed') return 'Recovery failed';
  return null;
}

function recoverySignal(state: string | null): TicketFlowStatusViewModel['signal'] | null {
  if (state === 'restarted') return 'active';
  if (state === 'recovering' || state === 'commit_barrier_pending') return 'waiting';
  if (state === 'commit_barrier_exhausted') return 'blocked';
  if (state === 'stale_alive') return 'blocked';
  if (state === 'restart_exhausted') return 'failed';
  if (state === 'failed') return 'failed';
  return null;
}

function reasonFromTickets(tickets: TicketSummary[]): string | null {
  const ticket = tickets.find((item) => item.errors.length > 0);
  return ticket?.errors[0] ?? null;
}

function statusSignal(status: WorkStatus): TicketFlowStatusViewModel['signal'] {
  if (status === 'running') return 'active';
  if (status === 'waiting') return 'waiting';
  if (status === 'blocked') return 'blocked';
  if (status === 'failed') return 'failed';
  if (status === 'invalid') return 'invalid';
  if (status === 'done') return 'done';
  return 'idle';
}

function formatElapsed(seconds: number | null): string {
  if (seconds === null) return 'Unknown';
  const safeSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const remainingSeconds = safeSeconds % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

function formatCount(value: number | null): string {
  return value === null ? 'Unknown' : String(value);
}

function numberFromRaw(raw: Record<string, unknown> | undefined, keys: string[]): number | null {
  if (!raw) return null;
  for (const key of keys) {
    const value = rawValue(raw, key);
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function mostRecent(values: (string | null)[]): string | null {
  return values
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => (Date.parse(right) || 0) - (Date.parse(left) || 0))[0] ?? null;
}

function stringFromRaw(raw: Record<string, unknown>, key: string): string | null {
  const value = rawValue(raw, key);
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function rawValue(raw: Record<string, unknown>, key: string): unknown {
  return key.split('.').reduce<unknown>((cursor, part) => {
    if (!cursor || typeof cursor !== 'object' || Array.isArray(cursor)) return undefined;
    return (cursor as Record<string, unknown>)[part];
  }, raw);
}

function aliasVariants(value: string): string[] {
  const normalized = normalizeAlias(value);
  const noExt = normalized.replace(/\.md$/, '');
  const basename = noExt.split('/').pop() ?? noExt;
  return [...new Set([normalized, noExt, basename])];
}

function normalizeAlias(value: string): string {
  return value.trim().replace(/\\/g, '/').toLowerCase();
}
