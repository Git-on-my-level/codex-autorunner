import type { PmaChatSummary, PmaRunProgress, PmaTimelineItem, SurfaceArtifact, TicketDetail, TicketSummary, WorkStatus } from './domain';
import {
  buildManagedThreadCreatePayload,
  buildPmaTranscriptCards,
  formatRelativeTime,
  pmaChatKind,
  pmaChatKindLabel,
  progressPercent,
  statusLabel,
  type PmaCard,
  type PmaChatKind,
  type ManagedThreadCreatePayload,
  type PmaChatScopeOption
} from './pmaChat';
import { repoRoute, repoTicketRoute, worktreeRoute, worktreeTicketRoute } from './routes';
import {
  aliasesOverlap,
  buildTicketFlowStatusViewModel,
  ticketAliases,
  ticketAliasesFromRun,
  type TicketFlowStatusViewModel
} from './ticketFlowStatus';

export type TicketFilter = 'all' | 'needs_attention' | 'active' | 'waiting' | 'failed' | 'open' | 'done_recent';

export type TicketSourceData = {
  tickets: TicketSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  artifacts: SurfaceArtifact[];
  timeline?: PmaTimelineItem[];
};

export type TicketWorkerActivityItem = {
  id: string;
  title: string;
  summary: string | null;
  detail: string | null;
  status: WorkStatus;
  timestamp: string | null;
};

export type TicketWorkerActivity = {
  items: TicketWorkerActivityItem[];
};

export type TicketEditPayload = {
  title: string;
  agent: string;
  model: string;
  reasoning: string;
  done: boolean;
  frontmatterYaml?: string;
  body: string;
};

export type TicketOwnerScope = {
  kind: 'repo' | 'worktree';
  id: string;
  /** Present for worktree queues mounted under `/repos/{repo}/worktrees/{wt}/tickets`. */
  parentRepoId?: string | null;
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
  diffStats: TicketSummary['diffStats'];
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

export type TicketQueueAction = {
  action: 'start' | 'stop' | 'restart';
  enabled: boolean;
  label: string;
  requiresConfirmation: boolean;
  disabledReason: string | null;
  method: 'GET' | 'POST';
  route: string | null;
};

export type SurfaceActionManifestAction = {
  action_id?: string;
  label?: string;
  enabled?: boolean;
  disabled_reason?: string | null;
  requires_confirmation?: boolean;
  method?: string;
  route?: string | null;
  tone?: string;
};

export type SurfaceActionManifest = {
  actions?: SurfaceActionManifestAction[];
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
  queueActions: TicketQueueAction[];
  flowStatus: TicketFlowStatusViewModel;
  /** Managed-thread id of the chat for the currently running ticket, when one exists. */
  currentChatId: string | null;
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

export type TicketLinkedChat = {
  id: string;
  title: string;
  status: WorkStatus;
  agentId: string | null;
  model: string | null;
  href: string;
  kind: PmaChatKind;
  kindLabel: string;
  updatedAt: string | null;
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
  /** Stored agent id for inline settings (frontmatter.agent / summary), not chat-derived labels. */
  agentRaw: string;
  /** Stored model id for inline settings — mirrors ticket markdown `model:` (API or body fallback). */
  modelRaw: string;
  /** Stored reasoning effort token for inline settings (empty = default / omit on save). */
  reasoningRaw: string;
  /** Changes when persisted ticket settings or body content relevant to sync changes (from server). */
  settingsSyncSignature: string;
  done: boolean;
  frontmatter: Record<string, unknown>;
  /** Serialized YAML view of `frontmatter` for read-only inspection (e.g. repair banner). */
  frontmatterYaml: string;
  /** Raw editable YAML frontmatter block from the ticket file when available. */
  frontmatterEditableYaml: string;
  /** Validation errors reported by the backend for this ticket; empty when valid. */
  errors: string[];
  /** True when the ticket cannot run until its frontmatter is fixed. */
  needsRepair: boolean;
  updatedLabel: string;
  goal: string | null;
  contractSections: TicketContractSection[];
  timeline: TicketTimelineItem[];
  progressPercent: number;
  artifacts: TicketArtifactRow[];
  chatTranscriptCards: PmaCard[];
  linkedChatId: string | null;
  /**
   * Every chat spawned for this ticket — typically one per agent
   * (Codex, OpenCode, etc.). Surfaced as pills under the ticket header so the
   * operator can jump to any of them without leaving the ticket page.
   */
  linkedChats: TicketLinkedChat[];
  chatHref: string | null;
  /** Flow id for ticket SSE / resume APIs — not a browser navigation target. */
  flowRunId: string | null;
  runHref: string | null;
  debugHref: string | null;
  actions: TicketAction[];
  rawBody: string;
  sourceTickets: TicketListRow[];
  previousTicketHref: string | null;
  nextTicketHref: string | null;
  raw: Record<string, unknown>;
};

const filterLabels: Record<TicketFilter, string> = {
  all: 'All',
  needs_attention: 'Needs attention',
  active: 'Active',
  waiting: 'Waiting',
  failed: 'Failed',
  open: 'Open',
  done_recent: 'Done/recent'
};

/** Chips shown on ticket list pages — broader filters stay available via `filterTicketRows` for tests/tools. */
const LIST_PAGE_FILTER_IDS: TicketFilter[] = ['all', 'open'];

export function buildTicketListViewModel(
  source: TicketSourceData,
  owner: TicketOwnerScope = null,
  actionManifest: SurfaceActionManifest | null = null
): TicketListViewModel {
  const rows = source.tickets.map((ticket) => ticketToListRow(ticket, source)).sort(owner ? byTicketNumberThenTitle : bySignalThenRecent);
  const ownerLabel = owner?.label || owner?.id;
  const queueRun = owner ? findQueueRun(source.runs, owner) ?? findQueueRunFromRows(rows) : null;
  const flowStatus = buildTicketFlowStatusViewModel(source.tickets, source.runs, owner);
  const activeCurrentTicketId = isTicketFlowActuallyWorking(flowStatus.status) ? flowStatus.currentTicketId : null;
  const rowsWithCurrent = rows.map((row) => ({ ...row, isCurrent: row.id === activeCurrentTicketId || row.routeId === activeCurrentTicketId }));
  const currentChatId = pickCurrentChatId(source.chats, flowStatus.currentTicketId);
  return {
    title: owner ? `${ownerLabel} tickets` : 'Tickets',
    eyebrow: owner ? `${owner.kind === 'repo' ? 'Repo' : 'Worktree'} ticket queue` : 'All-ticket projection',
    subtitle: owner
      ? 'This queue is read from this workspace’s .codex-autorunner/tickets directory.'
      : 'This projection spans known repos and worktrees. Tickets without a registered owner are flagged for ownership repair.',
    queueTitle: owner ? `${owner.kind === 'repo' ? 'Repo' : 'Worktree'} ticket queue` : 'All tickets',
    scopedOwner: owner,
    defaultFilter: 'all',
    defaultWorkspaceFilter: 'all',
    filters: LIST_PAGE_FILTER_IDS.map((id) => ({
      id,
      label: filterLabels[id],
      count: rowsWithCurrent.filter((row) => rowMatchesFilter(row, id)).length
    })),
    workspaceFilters: buildWorkspaceFilters(rowsWithCurrent),
    queueRun,
    queueActions: buildQueueActions(queueRun, source.runs, actionManifest),
    flowStatus,
    currentChatId,
    rows: rowsWithCurrent
  };
}

function pickCurrentChatId(chats: PmaChatSummary[], currentTicketId: string | null): string | null {
  if (!currentTicketId) return null;
  const matches = chats.filter((chat) => chat.ticketId === currentTicketId);
  if (matches.length === 0) return null;
  const activeStatuses: WorkStatus[] = ['running', 'waiting', 'blocked'];
  const active = matches.find((chat) => activeStatuses.includes(chat.status));
  if (active) return active.id;
  const sorted = [...matches].sort((a, b) => {
    const aTs = a.updatedAt ? new Date(a.updatedAt).getTime() : 0;
    const bTs = b.updatedAt ? new Date(b.updatedAt).getTime() : 0;
    return bTs - aTs;
  });
  return sorted[0]?.id ?? null;
}

function isTicketFlowActuallyWorking(status: WorkStatus): boolean {
  return status === 'running' || status === 'waiting' || status === 'blocked';
}

export function filterTicketRows(rows: TicketListRow[], filter: TicketFilter, workspaceFilter = 'all'): TicketListRow[] {
  return rows.filter((row) => rowMatchesFilter(row, filter) && rowMatchesWorkspaceFilter(row, workspaceFilter));
}

/** Raw `model` from API frontmatter or from a leading `---` / `---` block in the ticket body. */
export function ticketModelRawFromDetail(detail: TicketDetail): string {
  const fm = asRecord(detail.raw.frontmatter);
  const direct = stringFromRaw(fm, ['model']);
  if (direct) return direct;
  const block = extractFrontmatterYamlFromBody(detail.body);
  return parseYamlScalarFromBlock(block, 'model') ?? '';
}

/** Raw `model` from summary frontmatter or body YAML (falls back to linked chat in `ticketToListRow`). */
export function ticketModelRawFromSummary(ticket: TicketSummary): string {
  const fm = asRecord(ticket.raw.frontmatter);
  const direct = stringFromRaw(fm, ['model']);
  if (direct) return direct;
  const block = extractFrontmatterYamlFromBody(bodyFromTicketSummary(ticket));
  return parseYamlScalarFromBlock(block, 'model') ?? '';
}

/** Raw `reasoning` effort token from API frontmatter or from the body header block. */
export function ticketReasoningRawFromDetail(detail: TicketDetail): string {
  const fm = asRecord(detail.raw.frontmatter);
  const direct = stringFromRaw(fm, ['reasoning']);
  if (direct) return direct;
  const block = extractFrontmatterYamlFromBody(detail.body);
  return parseYamlScalarFromBlock(block, 'reasoning') ?? '';
}

/** Stored agent id for editing: YAML `agent` wins over summary `agentId`. */
export function ticketAgentRawFromDetail(detail: TicketDetail, frontmatter: Record<string, unknown>): string {
  const fromFm = stringFromRaw(frontmatter, ['agent']);
  if (fromFm) return fromFm;
  if (detail.agentId) return detail.agentId;
  return '';
}

function buildTicketSettingsSyncSignature(
  detail: TicketDetail,
  frontmatter: Record<string, unknown>,
  modelRaw: string,
  reasoningRaw: string,
  agentRaw: string
): string {
  const title = stringFromRaw(frontmatter, ['title']) ?? detail.title;
  return JSON.stringify({
    agent: agentRaw,
    model: modelRaw,
    reasoning: reasoningRaw,
    done: Boolean(frontmatter.done),
    title,
    bodyLen: detail.body.length
  });
}

function extractFrontmatterYamlFromBody(markdown: string): string | null {
  const text = markdown.replace(/\r\n/g, '\n');
  if (!text.startsWith('---\n')) return null;
  const rest = text.slice(4);
  const end = rest.indexOf('\n---\n');
  if (end !== -1) return rest.slice(0, end);
  const endAlt = rest.indexOf('\n---');
  if (endAlt === -1) return null;
  return rest.slice(0, endAlt);
}

function parseYamlScalarFromBlock(block: string | null, key: string): string | null {
  if (!block || !/^[a-zA-Z0-9_]+$/.test(key)) return null;
  for (const line of block.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const match = trimmed.match(new RegExp(`^${key}\\s*:\\s*(.*)$`));
    if (!match) continue;
    let value = match[1].trim();
    if (!value) return null;
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    return value || null;
  }
  return null;
}

function ticketFrontmatterYamlFromDetail(detail: TicketDetail, frontmatter: Record<string, unknown>): string {
  const rawYaml = detail.raw.frontmatter_yaml;
  if (typeof rawYaml === 'string') return rawYaml;
  const bodyYaml = extractFrontmatterYamlFromBody(detail.body);
  if (bodyYaml !== null) return bodyYaml;
  return serializeFrontmatter(frontmatter).trimEnd();
}

function upsertYamlScalar(block: string, key: string, value: string | boolean | null): string {
  const lines = block.replace(/\r\n/g, '\n').split('\n');
  const keyPattern = new RegExp(`^${key}\\s*:`);
  const replacement = value === null ? null : `${key}: ${yamlScalar(value)}`;
  const nextLines: string[] = [];
  let replaced = false;
  for (const line of lines) {
    if (keyPattern.test(line)) {
      replaced = true;
      if (replacement !== null) nextLines.push(replacement);
    } else {
      nextLines.push(line);
    }
  }
  if (!replaced && replacement !== null) nextLines.push(replacement);
  return nextLines.join('\n').trimEnd();
}

export function buildTicketDetailViewModel(
  detail: TicketDetail,
  source: TicketSourceData,
  now = new Date()
): TicketDetailViewModel {
  const run = findTicketRun(detail, source.runs);
  const allLinkedChats = findTicketChats(detail, source.chats, run);
  const chat = allLinkedChats[0] ?? null;
  const runArtifacts = [...detail.artifacts, ...source.artifacts, ...(run?.events ?? [])];
  const sections = parseTicketContract(detail.body);
  const goal = sectionText(sections, 'goal') || stringFromRaw(detail.raw, ['frontmatter.goal', 'goal']);
  const progress = run ? progressPercent(chat ?? syntheticChat(detail, run), run) : detail.status === 'done' ? 100 : 0;
  const flowRunId = run?.id ?? detail.runId ?? null;
  const runHref = flowRunId ? `/api/flows/${encodeURIComponent(flowRunId)}/status` : null;
  const debugHref = run ? `/api/flows/${encodeURIComponent(run.id)}/dispatch_history` : null;
  const chatHref = chat ? `/chats?chat=${encodeURIComponent(chat.id)}` : detail.chatKey ? `/chats?chat=${encodeURIComponent(detail.chatKey)}` : null;
  const linkedChatId = chat?.id ?? null;
  const chatTranscriptCards = buildPmaTranscriptCards(source.timeline ?? [], chat, [...detail.artifacts, ...source.artifacts], run);
  const sourceTickets = source.tickets.map((ticket) => ticketToListRow(ticket, source)).sort(byTicketNumberThenTitle);
  const routeId = routeIdForTicket(detail);
  const selectedIndex = sourceTickets.findIndex((row) => row.routeId === routeId || row.id === detail.id);
  const frontmatter = asRecord(detail.raw.frontmatter);
  const modelRaw = ticketModelRawFromDetail(detail);
  const reasoningRaw = ticketReasoningRawFromDetail(detail);
  const agentRaw = ticketAgentRawFromDetail(detail, frontmatter);
  const settingsSyncSignature = buildTicketSettingsSyncSignature(detail, frontmatter, modelRaw, reasoningRaw, agentRaw);

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
    modelLabel: modelRaw || null,
    reasoningLabel: reasoningRaw || null,
    agentRaw,
    modelRaw,
    reasoningRaw,
    settingsSyncSignature,
    done: Boolean(frontmatter.done),
    frontmatter,
    frontmatterYaml: serializeFrontmatter(frontmatter),
    frontmatterEditableYaml: ticketFrontmatterYamlFromDetail(detail, frontmatter),
    errors: [...detail.errors],
    needsRepair: detail.errors.length > 0 || (run?.status ?? detail.status) === 'invalid',
    updatedLabel: formatRelativeTime(detail.updatedAt ?? run?.lastEventAt ?? null, now),
    goal,
    contractSections: sections,
    timeline: buildTimeline(detail, run, runArtifacts, chatHref),
    progressPercent: progress,
    artifacts: uniqueArtifacts(runArtifacts).slice(0, 8).map(artifactToRow),
    chatTranscriptCards,
    linkedChatId,
    linkedChats: allLinkedChats.map(linkedChatToVm),
    chatHref,
    flowRunId,
    runHref,
    debugHref,
    actions: buildActions(chatHref, debugHref, run?.status ?? detail.status),
    rawBody: detail.body,
    sourceTickets,
    previousTicketHref: selectedIndex > 0 ? sourceTickets[selectedIndex - 1].href : null,
    nextTicketHref: selectedIndex >= 0 && selectedIndex < sourceTickets.length - 1 ? sourceTickets[selectedIndex + 1].href : null,
    raw: detail.raw
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
  const rawFrontmatter = payload.frontmatterYaml ?? detail.frontmatterEditableYaml;
  let frontmatterYaml = rawFrontmatter.trim();
  if (frontmatterYaml) {
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'title', payload.title.trim() || detail.title);
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'agent', payload.agent.trim() || 'codex');
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'done', payload.done);
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'model', payload.model.trim() || null);
    frontmatterYaml = upsertYamlScalar(frontmatterYaml, 'reasoning', payload.reasoning.trim() || null);
  } else {
    const frontmatter = { ...detail.frontmatter };
    frontmatter.title = payload.title.trim() || detail.title;
    frontmatter.agent = payload.agent.trim() || 'codex';
    frontmatter.done = payload.done;
    setOptional(frontmatter, 'model', payload.model.trim());
    setOptional(frontmatter, 'reasoning', payload.reasoning.trim());
    frontmatterYaml = serializeFrontmatter(frontmatter).trimEnd();
  }
  return `---\n${frontmatterYaml}\n---\n\n${payload.body.trimEnd()}\n`;
}

function stringField(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function ticketRepairChatScope(ticket: TicketDetailViewModel): PmaChatScopeOption {
  const parentRepoId =
    stringField(ticket.raw, 'repo_id') ??
    stringField(ticket.raw, 'base_repo_id') ??
    stringField(ticket.frontmatter, 'repo_id') ??
    stringField(ticket.frontmatter, 'base_repo_id');
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    const workspaceRoot = stringField(ticket.raw, 'workspace_root') ?? ticket.workspacePathLabel ?? '.';
    return {
      id: `worktree:${ticket.workspaceId}`,
      kind: 'worktree',
      label: ticket.workspaceId,
      detail: `Worktree · ${parentRepoId ?? ticket.workspaceId}`,
      workspaceRoot,
      resourceId: ticket.workspaceId,
      parentRepoId,
      scopeUrn: parentRepoId ? `worktree:${parentRepoId}/${ticket.workspaceId}` : `filesystem:${encodeURIComponent(workspaceRoot)}`
    };
  }
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return {
      id: `repo:${ticket.workspaceId}`,
      kind: 'repo',
      label: ticket.workspaceId,
      detail: `Repo · ${ticket.workspaceId}`,
      resourceKind: 'repo',
      resourceId: ticket.workspaceId,
      scopeUrn: `repo:${ticket.workspaceId}`
    };
  }
  return { id: 'local', kind: 'local', label: 'Local hub', detail: 'Current workspace', scopeUrn: 'hub' };
}

export function buildTicketRepairChatCreatePayload(ticket: TicketDetailViewModel): ManagedThreadCreatePayload {
  return buildManagedThreadCreatePayload('codex', ticketRepairChatScope(ticket), `Repair ${ticket.numberLabel} frontmatter`);
}

export function buildTicketRepairPrompt(ticket: TicketDetailViewModel): string {
  const raw = ticket.raw;
  const hubRoot = stringField(raw, 'hub_root') ?? '(hub root from the serving CAR instance)';
  const workspaceRoot = stringField(raw, 'workspace_root') ?? ticket.workspacePathLabel ?? '(unknown workspace root)';
  const ticketPath = ticket.pathLabel ?? '(unknown ticket path)';
  const errors = ticket.errors.length ? ticket.errors.map((err) => `- ${err}`).join('\n') : '- Frontmatter validation failed';
  return `Please repair this CAR ticket frontmatter and lint the ticket queue.\n\nHub root: ${hubRoot}\nWorkspace root: ${workspaceRoot}\nTicket path: ${ticketPath}\nAbsolute ticket path: ${workspaceRoot}/${ticketPath}\n\nValidation errors:\n${errors}\n\nRequirements:\n- Edit only the ticket file unless linting reveals directly related ticket metadata issues.\n- Fix the YAML frontmatter so the ticket can run.\n- Preserve the ticket body content.\n- Run: python3 .codex-autorunner/bin/lint_tickets.py from the workspace root.\n- Report exactly what changed and the lint result.`;
}

export function mergeTicketRunProgress(runs: PmaRunProgress[], progress: PmaRunProgress | null): PmaRunProgress[] {
  if (!progress) return runs;
  const index = runs.findIndex((run) => run.id === progress.id);
  if (index === -1) return [progress, ...runs];
  return runs.map((run, runIndex) => (runIndex === index ? progress : run));
}

export function buildTicketWorkerActivity(
  dispatchHistory: Record<string, unknown>[] = [],
  flowEvents: Record<string, unknown>[] = []
): TicketWorkerActivity {
  const items: TicketWorkerActivityItem[] = [];
  const liveText = flowEvents
    .filter((event) => stringFromRaw(event, ['event_type']) === 'agent_stream_delta')
    .map((event) => stringFromRaw(asRecord(event.data), ['delta']) ?? '')
    .join('')
    .trim();
  if (liveText) {
    items.push({
      id: 'live-worker-output',
      title: 'Live worker output',
      summary: null,
      detail: liveText,
      status: 'running',
      timestamp: latestFlowEventTimestamp(flowEvents)
    });
  }
  for (const event of flowEvents) {
    const eventType = stringFromRaw(event, ['event_type']);
    if (eventType === 'agent_stream_delta') continue;
    if (eventType === 'step_started') {
      const stepName = stringFromRaw(asRecord(event.data), ['step_name']) ?? 'Step started';
      items.push(workerItem(`event-${event.seq ?? items.length}`, stepName, null, null, 'running', stringFromRaw(event, ['timestamp'])));
    } else if (eventType === 'app_server_event') {
      const data = asRecord(event.data);
      items.push(workerItem(
        `event-${event.seq ?? items.length}`,
        stringFromRaw(data, ['title', 'method', 'type', 'event_type']) ?? 'Worker event',
        stringFromRaw(data, ['summary', 'message']),
        compactJson(data),
        'running',
        stringFromRaw(event, ['timestamp'])
      ));
    }
  }
  for (const entry of dispatchHistory) {
    const dispatch = asRecord(entry.dispatch);
    const errors = Array.isArray(entry.errors) ? entry.errors.filter((value): value is string => typeof value === 'string') : [];
    const attachments = Array.isArray(entry.attachments) ? entry.attachments : [];
    const diff = asRecord(dispatch.diff_stats);
    const diffParts = [
      typeof diff.insertions === 'number' ? `+${diff.insertions}` : null,
      typeof diff.deletions === 'number' ? `-${diff.deletions}` : null,
      typeof diff.files_changed === 'number' ? `${diff.files_changed} files` : null
    ].filter(Boolean);
    const summaryParts = [
      stringFromRaw(dispatch, ['body']),
      diffParts.length ? diffParts.join(' ') : null,
      attachments.length ? `${attachments.length} attachment${attachments.length === 1 ? '' : 's'}` : null,
      errors.length ? errors.join('; ') : null
    ].filter((value): value is string => Boolean(value));
    items.push(workerItem(
      `dispatch-${String(entry.seq ?? items.length)}`,
      stringFromRaw(dispatch, ['title', 'mode']) ?? `Dispatch ${String(entry.seq ?? '')}`.trim(),
      summaryParts.join(' · ') || null,
      null,
      errors.length ? 'failed' : 'done',
      stringFromRaw(entry, ['created_at'])
    ));
  }
  return { items: items.slice(0, 80) };
}

export function rowRelativeTime(row: { updatedAt?: string | null; createdAt?: string | null }, now = new Date()): string {
  return formatRelativeTime(row.updatedAt ?? row.createdAt ?? null, now);
}

function ticketToListRow(ticket: TicketSummary, source: TicketSourceData): TicketListRow {
  const run = findTicketRun(ticket, source.runs);
  const chat = findTicketChat(ticket, source.chats, run);
  const status = run?.status ?? ticket.status;
  const scope = workspaceScope(ticket);
  const modelFromTicket = ticketModelRawFromSummary(ticket).trim();
  const modelFromChat = chat?.model?.trim() ?? '';
  const modelResolved = modelFromTicket || modelFromChat;
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
    modelLabel: modelResolved ? modelResolved : null,
    diffStats: ticket.diffStats,
    durationLabel: formatDuration(ticket.durationSeconds),
    bodyPreview: bodyPreview(ticket),
    status: ticket.status,
    currentRunState: run?.status ?? chat?.status ?? null,
    currentRunId: run?.id ?? null,
    updatedAt: ticket.updatedAt ?? run?.lastEventAt ?? chat?.updatedAt ?? null,
    chatHref: chat ? `/chats?chat=${encodeURIComponent(chat.id)}` : ticket.chatKey ? `/chats?chat=${encodeURIComponent(ticket.chatKey)}` : null,
    href: scopedTicketHref(ticket) ?? '/chats',
    needsAttention: ticket.errors.length > 0 || ['waiting', 'failed', 'blocked', 'invalid'].includes(status),
    isCurrent: false
  };
}

function workerItem(
  id: string,
  title: string,
  summary: string | null,
  detail: string | null,
  status: WorkStatus,
  timestamp: string | null
): TicketWorkerActivityItem {
  return { id, title, summary, detail, status, timestamp };
}

function latestFlowEventTimestamp(events: Record<string, unknown>[]): string | null {
  for (const event of [...events].reverse()) {
    const timestamp = stringFromRaw(event, ['timestamp']);
    if (timestamp) return timestamp;
  }
  return null;
}

function compactJson(value: Record<string, unknown>): string | null {
  const keys = Object.keys(value);
  if (!keys.length) return null;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return null;
  }
}

function findQueueRun(runs: PmaRunProgress[], owner: Exclude<TicketOwnerScope, null>): TicketQueueRun | null {
  const matchingRuns = runs.filter((run) => runMatchesOwner(run, owner) && !isPendingStopRequestedRun(run));
  return (
    matchingRuns.find((run) => run.status === 'running') ??
    matchingRuns.find((run) => run.status === 'waiting' || run.status === 'blocked') ??
    matchingRuns[0] ??
    null
  );
}

function isPendingStopRequestedRun(run: PmaRunProgress): boolean {
  const rawStatus = stringFromRaw(run.raw, ['status']) ?? run.status;
  const stopRequested = booleanFromRaw(run.raw, ['stop_requested']);
  return String(rawStatus).trim().toLowerCase() === 'pending' && stopRequested === true;
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

function buildQueueActions(
  queueRun: TicketQueueRun | null,
  runs: PmaRunProgress[],
  actionManifest: SurfaceActionManifest | null
): TicketQueueAction[] {
  const manifestActions = actionsFromManifest(actionManifest);
  if (manifestActions.length > 0) return orderQueueActions(manifestActions);
  const run = queueRun ? runs.find((candidate) => candidate.id === queueRun.id) ?? null : null;
  const rawActions = Array.isArray(run?.raw.action_policy) ? run.raw.action_policy : [];
  const projected = rawActions
    .map(queueActionFromPolicy)
    .filter((action): action is TicketQueueAction => action !== null);
  if (projected.length > 0) return orderQueueActions(projected);
  return [];
}

function actionsFromManifest(manifest: SurfaceActionManifest | null): TicketQueueAction[] {
  const rawActions = Array.isArray(manifest?.actions) ? manifest.actions : [];
  return rawActions
    .map(queueActionFromManifest)
    .filter((action): action is TicketQueueAction => action !== null);
}

function queueActionFromManifest(action: SurfaceActionManifestAction): TicketQueueAction | null {
  const rawId = typeof action.action_id === 'string' ? action.action_id : '';
  const name = rawId.replace(/^ticket_flow\./, '');
  if (name !== 'start' && name !== 'stop' && name !== 'restart') return null;
  return {
    action: name,
    enabled: action.enabled === true,
    label: typeof action.label === 'string' && action.label.trim() ? action.label : name,
    requiresConfirmation: action.requires_confirmation === true,
    disabledReason: typeof action.disabled_reason === 'string' && action.disabled_reason.trim() ? action.disabled_reason : null,
    method: action.method === 'GET' ? 'GET' : 'POST',
    route: typeof action.route === 'string' && action.route.trim() ? action.route : null
  };
}

function queueActionFromPolicy(value: unknown): TicketQueueAction | null {
  const action = asRecord(value);
  const visibility = asRecord(action.surface_visibility);
  if (visibility.queue !== true) return null;
  const name = stringFromRaw(action, ['action']);
  if (name !== 'start' && name !== 'stop' && name !== 'restart') return null;
  return {
    action: name,
    enabled: action.enabled === true,
    label: stringFromRaw(action, ['label']) ?? name,
    requiresConfirmation: action.requires_confirmation === true,
    disabledReason: stringFromRaw(action, ['disabled_reason']),
    method: stringFromRaw(action, ['method']) === 'GET' ? 'GET' : 'POST',
    route: stringFromRaw(action, ['route'])
  };
}

function orderQueueActions(actions: TicketQueueAction[]): TicketQueueAction[] {
  const byAction = new Map(actions.map((action) => [action.action, action]));
  return (['start', 'stop', 'restart'] as const)
    .map((action) => byAction.get(action))
    .filter((action): action is TicketQueueAction => Boolean(action));
}

function runMatchesOwner(run: PmaRunProgress, owner: Exclude<TicketOwnerScope, null>): boolean {
  const raw = run.raw;
  const resourceKind = stringFromRaw(raw, ['resource_kind', 'state.resource_kind', 'input_data.resource_kind']);
  const resourceId = stringFromRaw(raw, ['resource_id', 'state.resource_id', 'input_data.resource_id']);
  const explicitWorktreeId = stringFromRaw(raw, [
    'worktree_id',
    'worktree_repo_id',
    'state.worktree_id',
    'state.worktree_repo_id',
    'input_data.worktree_id',
    'input_data.worktree_repo_id',
    'state.ticket_engine.worktree_id'
  ]);
  if (owner.kind === 'repo') {
    if (resourceKind === 'worktree' || explicitWorktreeId) return false;
    const repoId = stringFromRaw(raw, ['repo_id', 'state.repo_id', 'input_data.repo_id']);
    return resourceId === owner.id || repoId === owner.id || (resourceKind === 'repo' && resourceId === owner.id);
  }
  const worktreeId = stringFromRaw(raw, [
    'worktree_id',
    'worktree_repo_id',
    'state.worktree_id',
    'state.worktree_repo_id',
    'input_data.worktree_id',
    'input_data.worktree_repo_id',
    'state.ticket_engine.worktree_id'
  ]);
  return resourceId === owner.id || worktreeId === owner.id || (resourceKind === 'worktree' && resourceId === owner.id);
}

function byTicketNumberThenTitle(a: TicketListRow, b: TicketListRow): number {
  const aNumber = Number(a.numberLabel.replace(/^#/, ''));
  const bNumber = Number(b.numberLabel.replace(/^#/, ''));
  if (Number.isFinite(aNumber) && Number.isFinite(bNumber) && aNumber !== bNumber) return aNumber - bNumber;
  return a.title.localeCompare(b.title);
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
  if (filter === 'all') return true;
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

function buildTimeline(
  detail: TicketDetail,
  run: PmaRunProgress | null,
  artifacts: SurfaceArtifact[],
  chatHref: string | null
): TicketTimelineItem[] {
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
      href: chatHref
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

function buildActions(chatHref: string | null, debugHref: string | null, status: WorkStatus): TicketAction[] {
  const actions: TicketAction[] = [];
  if (chatHref) actions.push({ label: 'Open chat', href: chatHref, secondary: false, command: null });
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

function linkedChatToVm(chat: PmaChatSummary): TicketLinkedChat {
  const kind = pmaChatKind(chat);
  return {
    id: chat.id,
    title: chat.title,
    status: chat.status,
    agentId: chat.agentId,
    model: chat.model,
    href: `/chats?chat=${encodeURIComponent(chat.id)}`,
    kind,
    kindLabel: pmaChatKindLabel(kind),
    updatedAt: chat.updatedAt
  };
}

function findTicketRun(ticket: TicketSummary, runs: PmaRunProgress[]): PmaRunProgress | null {
  const aliases = ticketAliases(ticket);
  return runs.find((run) => aliasesOverlap(aliases, ticketAliasesFromRun(run))) ?? null;
}

function findTicketChat(ticket: TicketSummary, chats: PmaChatSummary[], run: PmaRunProgress | null): PmaChatSummary | null {
  return findTicketChats(ticket, chats, run)[0] ?? null;
}

function findTicketChats(ticket: TicketSummary, chats: PmaChatSummary[], run: PmaRunProgress | null): PmaChatSummary[] {
  const aliases = ticketAliases(ticket);
  const matched = chats.filter((chat) => {
    if (run?.chatId && chat.id === run.chatId) return true;
    const chatAliases = [chat.ticketId, stringFromRaw(chat.raw, ['chat_key', 'ticket_path', 'current_ticket'])]
      .filter((value): value is string => typeof value === 'string' && value.length > 0)
      .map(normalizeAlias);
    return chatAliases.some((alias) => aliases.has(alias));
  });
  // Active run's chat first, then by recency, with the active chat retained as the canonical primary.
  return matched.sort((a, b) => {
    const aRun = run?.chatId === a.id ? 0 : 1;
    const bRun = run?.chatId === b.id ? 0 : 1;
    if (aRun !== bRun) return aRun - bRun;
    return (b.updatedAt ?? '').localeCompare(a.updatedAt ?? '');
  });
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
    lifecycleStatus: 'active',
    status: run.status,
    agentId: ticket.agentId,
    agentProfile: null,
    model: null,
    repoId: ticket.repoId,
    worktreeId: ticket.worktreeId,
    ticketId: ticket.id,
    isTicketFlow: true,
    progressPercent: null,
    updatedAt: ticket.updatedAt,
    raw: {}
  };
}

function routeIdForTicket(ticket: TicketSummary): string {
  return ticketRouteSegmentFromSummary(ticket);
}

/** URL segment for a ticket row or detail link (matches list/detail routing). */
export function ticketRouteSegmentFromSummary(ticket: Pick<TicketSummary, 'id' | 'number'>): string {
  return ticket.number != null ? String(ticket.number) : ticket.id;
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
  parentRepoId: string | null;
  label: string;
} {
  const parentRepoId = ticket.repoId ?? stringFromRaw(ticket.raw, ['repo_id', 'base_repo_id']) ?? stringFromRaw(asRecord(ticket.raw.frontmatter), ['repo_id', 'base_repo_id']);
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return { kind: 'repo', id: ticket.workspaceId, parentRepoId: null, label: `Repo: ${ticket.workspaceId}` };
  }
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    return { kind: 'worktree', id: ticket.workspaceId, parentRepoId, label: `Worktree: ${ticket.workspaceId}` };
  }
  const raw = ticket.raw;
  const frontmatter = asRecord(raw.frontmatter);
  const repoId =
    parentRepoId ??
    stringFromRaw(raw, ['repo_id', 'base_repo_id']) ??
    stringFromRaw(frontmatter, ['repo_id', 'base_repo_id']);
  const worktreeId =
    ticket.worktreeId ??
    stringFromRaw(raw, ['worktree_id', 'worktree_repo_id']) ??
    stringFromRaw(frontmatter, ['worktree_id', 'worktree_repo_id']);
  const resourceKind = stringFromRaw(raw, ['resource_kind']) ?? stringFromRaw(frontmatter, ['resource_kind']);
  const resourceId = stringFromRaw(raw, ['resource_id']) ?? stringFromRaw(frontmatter, ['resource_id']);
  if (worktreeId) return { kind: 'worktree', id: worktreeId, parentRepoId: repoId, label: `Worktree: ${worktreeId}` };
  if (repoId) return { kind: 'repo', id: repoId, parentRepoId: null, label: `Repo: ${repoId}` };
  if (resourceKind === 'worktree' && resourceId) return { kind: 'worktree', id: resourceId, parentRepoId: repoId, label: `Worktree: ${resourceId}` };
  if (resourceKind === 'repo' && resourceId) return { kind: 'repo', id: resourceId, parentRepoId: null, label: `Repo: ${resourceId}` };
  return { kind: 'unscoped', id: null, parentRepoId: null, label: 'Needs owner repair' };
}

function workspaceHref(ticket: TicketSummary): string | null {
  const scope = workspaceScope(ticket);
  if (scope.kind === 'repo' && scope.id) return repoRoute(scope.id);
  if (scope.kind === 'worktree' && scope.id) return worktreeRoute(scope.id, scope.parentRepoId);
  return null;
}

function ownerTicketListHref(ticket: TicketSummary): string | null {
  const scope = workspaceScope(ticket);
  if (scope.kind === 'repo' && scope.id) return repoTicketRoute(scope.id);
  if (scope.kind === 'worktree' && scope.id) return worktreeTicketRoute(scope.id, scope.parentRepoId);
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

function booleanFromRaw(raw: Record<string, unknown>, keys: string[]): boolean | null {
  for (const key of keys) {
    const value = key.split('.').reduce<unknown>((cursor, part) => {
      if (!cursor || typeof cursor !== 'object' || Array.isArray(cursor)) return undefined;
      return (cursor as Record<string, unknown>)[part];
    }, raw);
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number' && Number.isFinite(value)) return value !== 0;
    if (typeof value === 'string' && value.trim()) {
      const normalized = value.trim().toLowerCase();
      if (['1', 'true', 'yes'].includes(normalized)) return true;
      if (['0', 'false', 'no'].includes(normalized)) return false;
    }
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
