import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary, WorkStatus } from './domain';
import { formatRelativeTime, progressPercent, statusLabel } from './pmaChat';

export type TicketFilter = 'needs_attention' | 'active' | 'waiting' | 'failed' | 'open' | 'done_recent';

export type TicketSourceData = {
  tickets: TicketSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  artifacts: SurfaceArtifact[];
};

export type TicketListRow = {
  id: string;
  routeId: string;
  numberLabel: string;
  title: string;
  repoLabel: string;
  agentLabel: string;
  status: WorkStatus;
  currentRunState: WorkStatus | null;
  updatedAt: string | null;
  chatHref: string | null;
  href: string;
  needsAttention: boolean;
};

export type TicketListViewModel = {
  title: string;
  eyebrow: string;
  defaultFilter: TicketFilter;
  filters: { id: TicketFilter; label: string; count: number }[];
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
  agentLabel: string;
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
};

const filterLabels: Record<TicketFilter, string> = {
  needs_attention: 'Needs attention',
  active: 'Active',
  waiting: 'Waiting',
  failed: 'Failed',
  open: 'Open',
  done_recent: 'Done/recent'
};

export function buildTicketListViewModel(source: TicketSourceData): TicketListViewModel {
  const rows = source.tickets.map((ticket) => ticketToListRow(ticket, source)).sort(bySignalThenRecent);
  return {
    title: 'Tickets',
    eyebrow: 'Queue',
    defaultFilter: 'needs_attention',
    filters: (Object.keys(filterLabels) as TicketFilter[]).map((id) => ({
      id,
      label: filterLabels[id],
      count: rows.filter((row) => rowMatchesFilter(row, id)).length
    })),
    rows
  };
}

export function filterTicketRows(rows: TicketListRow[], filter: TicketFilter): TicketListRow[] {
  return rows.filter((row) => rowMatchesFilter(row, filter));
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

  return {
    id: detail.id,
    routeId: routeIdForTicket(detail),
    numberLabel: ticketNumberLabel(detail),
    title: detail.title,
    status: run?.status ?? detail.status,
    repoLabel: repoLabel(detail),
    agentLabel: detail.agentId ?? chat?.agentId ?? 'Unassigned',
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
    rawBody: detail.body
  };
}

export function rowRelativeTime(row: { updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.updatedAt ?? row.createdAt ?? null, now);
}

function ticketToListRow(ticket: TicketSummary, source: TicketSourceData): TicketListRow {
  const run = findTicketRun(ticket, source.runs);
  const chat = findTicketChat(ticket, source.chats, run);
  const status = run?.status ?? ticket.status;
  return {
    id: ticket.id,
    routeId: routeIdForTicket(ticket),
    numberLabel: ticketNumberLabel(ticket),
    title: ticket.title,
    repoLabel: repoLabel(ticket),
    agentLabel: ticket.agentId ?? chat?.agentId ?? 'Unassigned',
    status: ticket.status,
    currentRunState: run?.status ?? chat?.status ?? null,
    updatedAt: ticket.updatedAt ?? run?.lastEventAt ?? chat?.updatedAt ?? null,
    chatHref: chat ? `/pma?chat=${encodeURIComponent(chat.id)}` : ticket.chatKey ? `/pma?chat=${encodeURIComponent(ticket.chatKey)}` : null,
    href: `/tickets/${encodeURIComponent(routeIdForTicket(ticket))}`,
    needsAttention: ticket.errors.length > 0 || ['waiting', 'failed', 'blocked'].includes(status)
  };
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
  const actions: TicketAction[] = [{ label: chatHref ? 'Open PMA chat' : 'Ask PMA', href: chatHref ?? '/pma', secondary: false, command: null }];
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
  return runs.find((run) => ticketAliasesFromRun(run).some((alias) => aliases.has(alias))) ?? null;
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

function ticketAliases(ticket: TicketSummary): Set<string> {
  return new Set(
    [ticket.id, ticket.path, ticket.chatKey, ticket.number ? `TICKET-${String(ticket.number).padStart(3, '0')}.md` : null, ticket.number ? String(ticket.number) : null]
      .filter(Boolean)
      .map((value) => normalizeAlias(String(value)))
  );
}

function ticketAliasesFromRun(run: PmaRunProgress): string[] {
  return ['ticket_id', 'current_ticket_id', 'current_ticket', 'ticket_path']
    .map((key) => stringFromRaw(run.raw, [key, `ticket_engine.${key}`, `state.ticket_engine.${key}`, `state.${key}`]))
    .filter((value): value is string => Boolean(value))
    .map(normalizeAlias);
}

function normalizeAlias(value: string): string {
  return value.trim().toLowerCase().replace(/^.*\/(ticket-\d+.*\.md)$/i, '$1');
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

function ticketNumberLabel(ticket: TicketSummary): string {
  return ticket.number ? `#${ticket.number}` : ticket.path?.split('/').pop()?.replace(/\.md$/, '') ?? ticket.id;
}

function repoLabel(ticket: TicketSummary): string {
  return ticket.worktreeId ?? ticket.repoId ?? stringFromRaw(ticket.raw, ['worktree', 'repo', 'path']) ?? 'Current workspace';
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
