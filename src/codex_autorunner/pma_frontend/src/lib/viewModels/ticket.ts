import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary, WorkStatus } from './domain';
import { formatRelativeTime, progressPercent, statusLabel } from './pmaChat';
import {
  aliasesOverlap,
  buildTicketFlowStatusViewModel,
  ticketAliases,
  ticketAliasesFromRun,
  type TicketFlowStatusViewModel
} from './ticketFlowStatus';

export type TicketFilter = 'needs_attention' | 'active' | 'waiting' | 'failed' | 'open' | 'done_recent';

export type TicketSourceData = {
  tickets: TicketSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  artifacts: SurfaceArtifact[];
};

export type TicketEditPayload = {
  title: string;
  agent: string;
  model: string;
  reasoning: string;
  done: boolean;
  body: string;
};

export type TicketOwnerScope = {
  kind: 'repo' | 'worktree';
  id: string;
  label?: string | null;
} | null;

export type TicketListRow = {
  id: string;
  routeId: string;
  numberLabel: string;
  title: string;
  repoLabel: string;
  workspaceKind: 'repo' | 'worktree' | 'unscoped';
  workspaceId: string | null;
  workspaceHref: string | null;
  ownerTicketHref: string | null;
  pathLabel: string | null;
  agentLabel: string;
  modelLabel: string | null;
  diffLabel: string | null;
  durationLabel: string | null;
  bodyPreview: string | null;
  status: WorkStatus;
  currentRunState: WorkStatus | null;
  currentRunId: string | null;
  updatedAt: string | null;
  chatHref: string | null;
  href: string;
  needsAttention: boolean;
  isCurrent: boolean;
};

export type TicketQueueRun = {
  id: string;
  status: WorkStatus;
};

export type TicketListViewModel = {
  title: string;
  eyebrow: string;
  subtitle: string;
  queueTitle: string;
  scopedOwner: TicketOwnerScope;
  defaultFilter: TicketFilter;
  defaultWorkspaceFilter: string;
  filters: { id: TicketFilter; label: string; count: number }[];
  workspaceFilters: { id: string; label: string; count: number }[];
  queueRun: TicketQueueRun | null;
  flowStatus: TicketFlowStatusViewModel;
  rows: TicketListRow[];
};

export type TicketContractSection = {
  id: string;
  title: string;
  items: string[];
  body: string;
};

export type TicketTimelineItem = {
  id: string;
  title: string;
  status: WorkStatus;
  summary: string;
  timestamp: string | null;
  href: string | null;
};

export type TicketAction = {
  label: string;
  href: string | null;
  secondary: boolean;
  command: 'resume' | 'bootstrap' | null;
};

export type TicketArtifactRow = {
  id: string;
  title: string;
  summary: string;
  kind: SurfaceArtifact['kind'];
  href: string | null;
  createdAt: string | null;
};

export type TicketDetailViewModel = {
  id: string;
  routeId: string;
  numberLabel: string;
  title: string;
  status: WorkStatus;
  repoLabel: string;
  workspaceKind: 'repo' | 'worktree' | 'unscoped';
  workspaceId: string | null;
  workspaceHref: string | null;
  ownerTicketListHref: string | null;
  pathLabel: string | null;
  workspacePathLabel: string | null;
  agentLabel: string;
  modelLabel: string | null;
  reasoningLabel: string | null;
  done: boolean;
  frontmatter: Record<string, unknown>;
  updatedLabel: string;
  goal: string | null;
  contractSections: TicketContractSection[];
  timeline: TicketTimelineItem[];
  progressPercent: number;
  artifacts: TicketArtifactRow[];
  chatHref: string | null;
  runHref: string | null;
  debugHref: string | null;
  actions: TicketAction[];
  rawBody: string;
  sourceTickets: TicketListRow[];
  previousTicketHref: string | null;
  nextTicketHref: string | null;
};

const filterLabels: Record<TicketFilter, string> = {
  needs_attention: 'Needs attention',
  active: 'Active',
  waiting: 'Waiting',
  failed: 'Failed',
  open: 'Open',
  done_recent: 'Done/recent'
};

export function buildTicketListViewModel(source: TicketSourceData, owner: TicketOwnerScope = null): TicketListViewModel {
  const rows = source.tickets.map((ticket) => ticketToListRow(ticket, source)).sort(owner ? byTicketNumberThenTitle : bySignalThenRecent);
  const ownerLabel = owner?.label || owner?.id;
  const queueRun = owner ? findQueueRun(source.runs, owner) ?? findQueueRunFromRows(rows) : null;
  const flowStatus = buildTicketFlowStatusViewModel(source.tickets, source.runs, owner);
  const rowsWithCurrent = rows.map((row) => ({ ...row, isCurrent: row.id === flowStatus.currentTicketId || row.routeId === flowStatus.currentTicketId }));
  return {
    title: owner ? `${ownerLabel} tickets` : 'Tickets',
    eyebrow: owner ? `${owner.kind === 'repo' ? 'Repo' : 'Worktree'} ticket queue` : 'All-ticket projection',
    subtitle: owner
      ? 'This queue is read from this workspace’s .codex-autorunner/tickets directory.'
      : 'This projection spans known repos and worktrees. Tickets without a registered owner are flagged for ownership repair.',
    queueTitle: owner ? `${owner.kind === 'repo' ? 'Repo' : 'Worktree'} ticket queue` : 'All tickets',
    scopedOwner: owner,
    defaultFilter: 'open',
    defaultWorkspaceFilter: 'all',
    filters: (Object.keys(filterLabels) as TicketFilter[]).map((id) => ({
      id,
      label: filterLabels[id],
      count: rowsWithCurrent.filter((row) => rowMatchesFilter(row, id)).length
    })),
    workspaceFilters: buildWorkspaceFilters(rowsWithCurrent),
    queueRun,
    flowStatus,
    rows: rowsWithCurrent
  };
}

export function filterTicketRows(rows: TicketListRow[], filter: TicketFilter, workspaceFilter = 'all'): TicketListRow[] {
  return rows.filter((row) => rowMatchesFilter(row, filter) && rowMatchesWorkspaceFilter(row, workspaceFilter));
}

export function buildTicketDetailViewModel(
  detail: TicketDetail,
  source: TicketSourceData,
  now = new Date()
): TicketDetailViewModel {
  const run = findTicketRun(detail, source.runs);
  const chat = findTicketChat(detail, source.chats, run);
  const runArtifacts = [...detail.artifacts, ...source.artifacts, ...(run?.events ?? [])];
  const sections = parseTicketContract(detail.body);
  const goal = sectionText(sections, 'goal') || stringFromRaw(detail.raw, ['frontmatter.goal', 'goal']);
  const progress = run ? progressPercent(chat ?? syntheticChat(detail, run), run) : detail.status === 'done' ? 100 : 0;
  const runHref = run ? `/api/flows/${encodeURIComponent(run.id)}/status` : detail.runId ? `/api/flows/${encodeURIComponent(detail.runId)}/status` : null;
  const debugHref = run ? `/api/flows/${encodeURIComponent(run.id)}/dispatch_history` : null;
  const chatHref = chat ? `/pma?chat=${encodeURIComponent(chat.id)}` : detail.chatKey ? `/pma?chat=${encodeURIComponent(detail.chatKey)}` : null;
  const sourceTickets = source.tickets.map((ticket) => ticketToListRow(ticket, source)).sort(byTicketNumberThenTitle);
  const routeId = routeIdForTicket(detail);
  const selectedIndex = sourceTickets.findIndex((row) => row.routeId === routeId || row.id === detail.id);
  const frontmatter = asRecord(detail.raw.frontmatter);

  return {
    id: detail.id,
    routeId,
    numberLabel: ticketNumberLabel(detail),
    title: detail.title,
    status: run?.status ?? detail.status,
    repoLabel: repoLabel(detail),
    workspaceKind: workspaceScope(detail).kind,
    workspaceId: workspaceScope(detail).id,
    workspaceHref: workspaceHref(detail),
    ownerTicketListHref: ownerTicketListHref(detail),
    pathLabel: detail.path,
    workspacePathLabel: detail.workspacePath,
    agentLabel: detail.agentId ?? chat?.agentId ?? 'Unassigned',
    modelLabel: stringFromRaw(frontmatter, ['model']),
    reasoningLabel: stringFromRaw(frontmatter, ['reasoning']),
    done: Boolean(frontmatter.done),
    frontmatter,
    updatedLabel: formatRelativeTime(detail.updatedAt ?? run?.lastEventAt ?? null, now),
    goal,
    contractSections: sections,
    timeline: buildTimeline(detail, run, runArtifacts),
    progressPercent: progress,
    artifacts: uniqueArtifacts(runArtifacts).slice(0, 8).map(artifactToRow),
    chatHref,
    runHref,
    debugHref,
    actions: buildActions(chatHref, runHref, debugHref, run?.status ?? detail.status),
    rawBody: detail.body,
    sourceTickets,
    previousTicketHref: selectedIndex > 0 ? sourceTickets[selectedIndex - 1].href : null,
    nextTicketHref: selectedIndex >= 0 && selectedIndex < sourceTickets.length - 1 ? sourceTickets[selectedIndex + 1].href : null
  };
}

export function resolveTicketRouteId(tickets: TicketSummary[], routeId: string): TicketSummary | null {
  return resolveTicketRouteMatches(tickets, routeId)[0] ?? null;
}

export function resolveTicketRouteMatches(tickets: TicketSummary[], routeId: string): TicketSummary[] {
  const decoded = decodeURIComponent(routeId);
  const routeAliases = new Set([decoded, decoded.replace(/\.md$/i, ''), numericTicketAlias(decoded)].filter((value): value is string => Boolean(value)).flatMap((value) => [normalizeAlias(value), normalizeAlias(`${value}.md`)]));
  return tickets.filter((ticket) => aliasesOverlap(ticketAliases(ticket), routeAliases));
}

export function ticketDetailFromSummary(ticket: TicketSummary): TicketDetail {
  return {
    ...ticket,
    body: bodyFromTicketSummary(ticket),
    progress: null,
    artifacts: []
  };
}

export function buildTicketUpdateContent(detail: TicketDetailViewModel, payload: TicketEditPayload): string {
  const frontmatter = { ...detail.frontmatter };
  frontmatter.title = payload.title.trim() || detail.title;
  frontmatter.agent = payload.agent.trim() || 'codex';
  frontmatter.done = payload.done;
  setOptional(frontmatter, 'model', payload.model.trim());
  setOptional(frontmatter, 'reasoning', payload.reasoning.trim());
  return `---\n${serializeFrontmatter(frontmatter)}---\n\n${payload.body.trimEnd()}\n`;
}

export function rowRelativeTime(row: { updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.updatedAt ?? row.createdAt ?? null, now);
}

function ticketToListRow(ticket: TicketSummary, source: TicketSourceData): TicketListRow {
  const run = findTicketRun(ticket, source.runs);
  const chat = findTicketChat(ticket, source.chats, run);
  const status = run?.status ?? ticket.status;
  const scope = workspaceScope(ticket);
  return {
    id: ticket.id,
    routeId: routeIdForTicket(ticket),
    numberLabel: ticketNumberLabel(ticket),
    title: ticket.title,
    repoLabel: scope.label,
    workspaceKind: scope.kind,
    workspaceId: scope.id,
    workspaceHref: workspaceHref(ticket),
    ownerTicketHref: scopedTicketHref(ticket),
    pathLabel: ticket.path,
    agentLabel: ticket.agentId ?? chat?.agentId ?? 'Unassigned',
    modelLabel: stringFromRaw(asRecord(ticket.raw.frontmatter), ['model']),
    diffLabel: diffLabel(ticket),
    durationLabel: formatDuration(ticket.durationSeconds),
    bodyPreview: bodyPreview(ticket),
    status: ticket.status,
    currentRunState: run?.status ?? chat?.status ?? null,
    currentRunId: run?.id ?? null,
    updatedAt: ticket.updatedAt ?? run?.lastEventAt ?? chat?.updatedAt ?? null,
    chatHref: chat ? `/pma?chat=${encodeURIComponent(chat.id)}` : ticket.chatKey ? `/pma?chat=${encodeURIComponent(ticket.chatKey)}` : null,
    href: scopedTicketHref(ticket) ?? `/tickets/${encodeURIComponent(routeIdForTicket(ticket))}`,
    needsAttention: ticket.errors.length > 0 || ['waiting', 'failed', 'blocked'].includes(status),
    isCurrent: false
  };
}

function findQueueRun(runs: PmaRunProgress[], owner: Exclude<TicketOwnerScope, null>): TicketQueueRun | null {
  const matchingRuns = runs.filter((run) => runMatchesOwner(run, owner));
  return (
    matchingRuns.find((run) => run.status === 'running') ??
    matchingRuns.find((run) => run.status === 'waiting' || run.status === 'blocked') ??
    matchingRuns[0] ??
    null
  );
}

function findQueueRunFromRows(rows: TicketListRow[]): TicketQueueRun | null {
  const rowRuns = rows
    .filter((row): row is TicketListRow & { currentRunId: string; currentRunState: WorkStatus } => Boolean(row.currentRunId && row.currentRunState))
    .map((row) => ({ id: row.currentRunId, status: row.currentRunState }));
  return (
    rowRuns.find((run) => run.status === 'running') ??
    rowRuns.find((run) => run.status === 'waiting' || run.status === 'blocked') ??
    rowRuns[0] ??
    null
  );
}

function runMatchesOwner(run: PmaRunProgress, owner: Exclude<TicketOwnerScope, null>): boolean {
  const raw = run.raw;
  const resourceKind = stringFromRaw(raw, ['resource_kind', 'state.resource_kind', 'input_data.resource_kind']);
  const resourceId = stringFromRaw(raw, ['resource_id', 'state.resource_id', 'input_data.resource_id']);
  const repoId = stringFromRaw(raw, ['repo_id', 'base_repo_id', 'state.repo_id', 'state.base_repo_id', 'input_data.repo_id', 'input_data.base_repo_id']);
  const worktreeId = stringFromRaw(raw, ['worktree_id', 'worktree_repo_id', 'state.worktree_id', 'state.worktree_repo_id', 'input_data.worktree_id', 'input_data.worktree_repo_id']);
  if (owner.kind === 'repo') return resourceId === owner.id || repoId === owner.id || (resourceKind === 'repo' && resourceId === owner.id);
  return resourceId === owner.id || worktreeId === owner.id || (resourceKind === 'worktree' && resourceId === owner.id);
}

function byTicketNumberThenTitle(a: TicketListRow, b: TicketListRow): number {
  const aNumber = Number(a.numberLabel.replace(/^#/, ''));
  const bNumber = Number(b.numberLabel.replace(/^#/, ''));
  if (Number.isFinite(aNumber) && Number.isFinite(bNumber) && aNumber !== bNumber) return aNumber - bNumber;
  return a.title.localeCompare(b.title);
}

function diffLabel(ticket: TicketSummary): string | null {
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
  const body = bodyFromTicketSummary(ticket).replace(/\s+/g, ' ').trim();
  if (!body) return null;
  return body.length > 120 ? `${body.slice(0, 117)}...` : body;
}

function buildWorkspaceFilters(rows: TicketListRow[]): { id: string; label: string; count: number }[] {
  const filters = [{ id: 'all', label: 'All workspaces', count: rows.length }];
  const scoped = new Map<string, { id: string; label: string; count: number }>();
  for (const row of rows) {
    const id = row.workspaceKind === 'unscoped' ? 'unscoped' : `${row.workspaceKind}:${row.workspaceId}`;
    const label =
      row.workspaceKind === 'unscoped'
        ? 'Unscoped fallback'
        : `${row.workspaceKind === 'repo' ? 'Repo' : 'Worktree'} ${row.workspaceId}`;
    const current = scoped.get(id);
    scoped.set(id, { id, label, count: (current?.count ?? 0) + 1 });
  }
  return [...filters, ...[...scoped.values()].sort((a, b) => a.label.localeCompare(b.label))];
}

function rowMatchesFilter(row: TicketListRow, filter: TicketFilter): boolean {
  const runState = row.currentRunState;
  if (filter === 'needs_attention') return row.needsAttention;
  if (filter === 'active') return row.status === 'running' || runState === 'running';
  if (filter === 'waiting') return row.status === 'waiting' || row.status === 'blocked' || runState === 'waiting' || runState === 'blocked';
  if (filter === 'failed') return row.status === 'failed' || runState === 'failed';
  if (filter === 'open') return row.status !== 'done';
  return row.status === 'done';
}

function rowMatchesWorkspaceFilter(row: TicketListRow, workspaceFilter: string): boolean {
  if (workspaceFilter === 'all') return true;
  if (workspaceFilter === 'unscoped') return row.workspaceKind === 'unscoped';
  return `${row.workspaceKind}:${row.workspaceId}` === workspaceFilter;
}

function buildTimeline(detail: TicketDetail, run: PmaRunProgress | null, artifacts: SurfaceArtifact[]): TicketTimelineItem[] {
  const items: TicketTimelineItem[] = [];
  items.push({
    id: `ticket-${detail.id}`,
    title: 'Ticket contract loaded',
    status: detail.status,
    summary: `${ticketNumberLabel(detail)} · ${detail.agentId ?? 'agent unset'}`,
    timestamp: detail.updatedAt,
    href: null
  });
  if (run) {
    items.push({
      id: `run-${run.id}`,
      title: statusLabel(run.status),
      status: run.status,
      summary: [run.phase, run.guidance, run.queueDepth ? `${run.queueDepth} queued` : null].filter(Boolean).join(' · ') || 'Ticket flow run state',
      timestamp: run.lastEventAt,
      href: `/api/flows/${encodeURIComponent(run.id)}/status`
    });
    for (const event of run.events.slice(-4)) {
      items.push({
        id: `event-${event.id}`,
        title: event.title,
        status: event.kind === 'error' ? 'failed' : run.status,
        summary: event.summary ?? event.kind,
        timestamp: event.createdAt,
        href: event.url
      });
    }
  }
  for (const artifact of artifacts.filter((item) => item.kind !== 'progress').slice(0, 3)) {
    items.push({
      id: `artifact-${artifact.id}`,
      title: artifact.title,
      status: artifact.kind === 'error' ? 'failed' : 'done',
      summary: artifact.summary ?? artifact.kind,
      timestamp: artifact.createdAt,
      href: artifact.url
    });
  }
  return items;
}

function buildActions(chatHref: string | null, runHref: string | null, debugHref: string | null, status: WorkStatus): TicketAction[] {
  const actions: TicketAction[] = chatHref
    ? [{ label: 'Open PMA chat', href: chatHref, secondary: false, command: null }]
    : [];
  if (runHref) actions.push({ label: 'Open run', href: runHref, secondary: false, command: null });
  if (status === 'waiting' || status === 'blocked') actions.push({ label: 'Continue run', href: null, secondary: false, command: 'resume' });
  if (status === 'failed') actions.push({ label: 'Retry run', href: null, secondary: false, command: 'bootstrap' });
  if (debugHref) actions.push({ label: 'Raw logs/debug', href: debugHref, secondary: true, command: null });
  return actions;
}

export function parseTicketContract(markdown: string): TicketContractSection[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const sections: TicketContractSection[] = [];
  let current: TicketContractSection = { id: 'notes', title: 'Notes', items: [], body: '' };
  for (const line of lines) {
    const heading = line.match(/^##+\s+(.+?)\s*$/);
    if (heading) {
      if (current.body.trim() || current.items.length) sections.push(finishSection(current));
      const title = heading[1].trim();
      current = { id: slug(title), title, items: [], body: '' };
      continue;
    }
    const item = line.match(/^\s*-\s+(.*)$/);
    if (item) current.items.push(item[1].trim());
    else current.body += `${line}\n`;
  }
  if (current.body.trim() || current.items.length) sections.push(finishSection(current));
  return prioritizeContractSections(sections);
}

function prioritizeContractSections(sections: TicketContractSection[]): TicketContractSection[] {
  const preferred = ['goal', 'tasks', 'acceptance-criteria', 'tests', 'notes', 'scope-notes'];
  return [...sections].sort((a, b) => {
    const ai = preferred.indexOf(a.id);
    const bi = preferred.indexOf(b.id);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}

function finishSection(section: TicketContractSection): TicketContractSection {
  return { ...section, body: section.body.trim() };
}

function findTicketRun(ticket: TicketSummary, runs: PmaRunProgress[]): PmaRunProgress | null {
  const aliases = ticketAliases(ticket);
  return runs.find((run) => aliasesOverlap(aliases, ticketAliasesFromRun(run))) ?? null;
}

function findTicketChat(ticket: TicketSummary, chats: PmaChatSummary[], run: PmaRunProgress | null): PmaChatSummary | null {
  const aliases = ticketAliases(ticket);
  if (run?.chatId) {
    const byRun = chats.find((chat) => chat.id === run.chatId);
    if (byRun) return byRun;
  }
  return chats.find((chat) => {
    const chatAliases = [chat.ticketId, stringFromRaw(chat.raw, ['chat_key', 'ticket_path', 'current_ticket'])]
      .filter((value): value is string => typeof value === 'string' && value.length > 0)
      .map(normalizeAlias);
    return chatAliases.some((alias) => aliases.has(alias));
  }) ?? null;
}

function normalizeAlias(value: string): string {
  return value.trim().replace(/\\/g, '/').toLowerCase().replace(/^.*\/(ticket-\d+.*(?:\.md)?)$/i, '$1');
}

function numericTicketAlias(value: string): string | null {
  return /^\d+$/.test(value) ? `TICKET-${value.padStart(3, '0')}` : null;
}

function syntheticChat(ticket: TicketDetail, run: PmaRunProgress): PmaChatSummary {
  return {
    id: ticket.chatKey ?? ticket.id,
    title: ticket.title,
    status: run.status,
    agentId: ticket.agentId,
    model: null,
    repoId: ticket.repoId,
    worktreeId: ticket.worktreeId,
    ticketId: ticket.id,
    progressPercent: null,
    updatedAt: ticket.updatedAt,
    raw: {}
  };
}

function routeIdForTicket(ticket: TicketSummary): string {
  return ticket.number ? String(ticket.number) : ticket.id;
}

function bodyFromTicketSummary(ticket: TicketSummary): string {
  const rawBody = ticket.raw.body ?? ticket.raw.content ?? ticket.raw.markdown;
  return typeof rawBody === 'string' ? rawBody : '';
}

function ticketNumberLabel(ticket: TicketSummary): string {
  return ticket.number ? `#${ticket.number}` : ticket.path?.split('/').pop()?.replace(/\.md$/, '') ?? ticket.id;
}

function repoLabel(ticket: TicketSummary): string {
  return workspaceScope(ticket).label;
}

function workspaceScope(ticket: TicketSummary): {
  kind: 'repo' | 'worktree' | 'unscoped';
  id: string | null;
  label: string;
} {
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return { kind: 'repo', id: ticket.workspaceId, label: `Repo: ${ticket.workspaceId}` };
  }
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    return { kind: 'worktree', id: ticket.workspaceId, label: `Worktree: ${ticket.workspaceId}` };
  }
  const raw = ticket.raw;
  const frontmatter = asRecord(raw.frontmatter);
  const repoId =
    ticket.repoId ??
    stringFromRaw(raw, ['repo_id', 'base_repo_id']) ??
    stringFromRaw(frontmatter, ['repo_id', 'base_repo_id']);
  const worktreeId =
    ticket.worktreeId ??
    stringFromRaw(raw, ['worktree_id', 'worktree_repo_id']) ??
    stringFromRaw(frontmatter, ['worktree_id', 'worktree_repo_id']);
  const resourceKind = stringFromRaw(raw, ['resource_kind']) ?? stringFromRaw(frontmatter, ['resource_kind']);
  const resourceId = stringFromRaw(raw, ['resource_id']) ?? stringFromRaw(frontmatter, ['resource_id']);
  if (worktreeId) return { kind: 'worktree', id: worktreeId, label: `Worktree: ${worktreeId}` };
  if (repoId) return { kind: 'repo', id: repoId, label: `Repo: ${repoId}` };
  if (resourceKind === 'worktree' && resourceId) return { kind: 'worktree', id: resourceId, label: `Worktree: ${resourceId}` };
  if (resourceKind === 'repo' && resourceId) return { kind: 'repo', id: resourceId, label: `Repo: ${resourceId}` };
  return { kind: 'unscoped', id: null, label: 'Needs owner repair' };
}

function workspaceHref(ticket: TicketSummary): string | null {
  const scope = workspaceScope(ticket);
  if (scope.kind === 'repo' && scope.id) return `/repos/${encodeURIComponent(scope.id)}`;
  if (scope.kind === 'worktree' && scope.id) return `/worktrees/${encodeURIComponent(scope.id)}`;
  return null;
}

function ownerTicketListHref(ticket: TicketSummary): string | null {
  const scope = workspaceScope(ticket);
  if (scope.kind === 'repo' && scope.id) return `/repos/${encodeURIComponent(scope.id)}/tickets`;
  if (scope.kind === 'worktree' && scope.id) return `/worktrees/${encodeURIComponent(scope.id)}/tickets`;
  return null;
}

function scopedTicketHref(ticket: TicketSummary): string | null {
  const base = ownerTicketListHref(ticket);
  return base ? `${base}/${encodeURIComponent(routeIdForTicket(ticket))}` : null;
}

function artifactToRow(artifact: SurfaceArtifact): TicketArtifactRow {
  return {
    id: artifact.id,
    title: artifact.title,
    summary: artifact.summary ?? artifact.kind,
    kind: artifact.kind,
    href: artifact.url,
    createdAt: artifact.createdAt
  };
}

function uniqueArtifacts(artifacts: SurfaceArtifact[]): SurfaceArtifact[] {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    const key = `${artifact.kind}:${artifact.url ?? artifact.id}:${artifact.title}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function sectionText(sections: TicketContractSection[], id: string): string | null {
  const section = sections.find((item) => item.id === id);
  if (!section) return null;
  return section.body || section.items.join('\n') || null;
}

function stringFromRaw(raw: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = key.split('.').reduce<unknown>((cursor, part) => {
      if (!cursor || typeof cursor !== 'object' || Array.isArray(cursor)) return undefined;
      return (cursor as Record<string, unknown>)[part];
    }, raw);
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return null;
}

function setOptional(target: Record<string, unknown>, key: string, value: string): void {
  if (value) target[key] = value;
  else delete target[key];
}

function serializeFrontmatter(frontmatter: Record<string, unknown>): string {
  const preferred = ['agent', 'done', 'ticket_id', 'title', 'goal', 'profile', 'model', 'reasoning'];
  const keys = [
    ...preferred.filter((key) => Object.prototype.hasOwnProperty.call(frontmatter, key)),
    ...Object.keys(frontmatter).filter((key) => !preferred.includes(key)).sort()
  ];
  return keys.map((key) => `${key}: ${yamlScalar(frontmatter[key])}\n`).join('');
}

function yamlScalar(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value) || typeof value === 'object') return JSON.stringify(value);
  return JSON.stringify(String(value));
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function slug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'section';
}

function bySignalThenRecent(a: TicketListRow, b: TicketListRow): number {
  const signal = Number(b.needsAttention) - Number(a.needsAttention);
  if (signal !== 0) return signal;
  const active = Number(b.currentRunState === 'running') - Number(a.currentRunState === 'running');
  if (active !== 0) return active;
  return new Date(b.updatedAt ?? 0).getTime() - new Date(a.updatedAt ?? 0).getTime();
}
