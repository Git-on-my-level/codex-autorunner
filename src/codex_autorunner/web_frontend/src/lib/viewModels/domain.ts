type JsonRecord = Record<string, unknown>;

export type WorkStatus = 'running' | 'waiting' | 'idle' | 'done' | 'failed' | 'blocked' | 'invalid';

/** Stable chat-list shape consumed by PMA room components. */
export type PmaChatSummary = {
  id: string;
  title: string;
  lifecycleStatus: string | null;
  status: WorkStatus;
  agentId: string | null;
  /** Hermes (and similar) runtime profile when set on the managed thread. */
  agentProfile: string | null;
  model: string | null;
  repoId: string | null;
  worktreeId: string | null;
  ticketId: string | null;
  ticketDone?: boolean | null;
  ticketPath?: string | null;
  runId?: string | null;
  flowType?: string | null;
  /** True when the chat originates from a ticket-flow run (raw name like `ticket-flow:codex`,
   * a CAR_TICKET_FLOW_PROMPT control message, or a populated current_ticket_id). Used to
   * collapse run rows by repo/worktree even when a specific ticket id isn't surfaced. */
  isTicketFlow: boolean;
  progressPercent: number | null;
  updatedAt: string | null;
  raw: JsonRecord;
};

/** Normalized user/PMA message shape, including surfaced cards as child artifacts. */
export type PmaChatMessage = {
  id: string;
  chatId: string | null;
  role: 'user' | 'assistant' | 'system' | 'tool';
  text: string;
  createdAt: string | null;
  status: WorkStatus | null;
  artifacts: SurfaceArtifact[];
  raw: JsonRecord;
};

export type PmaTimelineItemKind =
  | 'user_message'
  | 'assistant_message'
  | 'intermediate'
  | 'tool_group'
  | 'status'
  | 'approval'
  | 'artifact'
  | 'delivery_state'
  | 'lifecycle';

/** Backend-owned PMA chat timeline item with stable reconciliation identity. */
export type PmaTimelineItem = {
  id: string;
  kind: PmaTimelineItemKind;
  orderKey: string;
  timestamp: string | null;
  chatId: string | null;
  turnId: string | null;
  status: WorkStatus | null;
  payload: JsonRecord;
  raw: JsonRecord;
};

/** Compact run/tail/status state for chat, dashboard, repo, and ticket surfaces. */
export type PmaRunProgress = {
  id: string;
  chatId: string | null;
  status: WorkStatus;
  workStatus: string | null;
  operatorStatus: string | null;
  terminal: boolean;
  streamShouldClose: boolean;
  streamCloseReason: string | null;
  phase: string | null;
  guidance: string | null;
  queueDepth: number;
  elapsedSeconds: number | null;
  idleSeconds: number | null;
  lastEventId: number | null;
  lastEventAt: string | null;
  progressPercent: number | null;
  events: SurfaceArtifact[];
  raw: JsonRecord;
};

/** UI-facing artifact card, independent of the backend source route. */
export type SurfaceArtifact = {
  id: string;
  kind:
    | 'screenshot'
    | 'image'
    | 'file'
    | 'preview_url'
    | 'test_result'
    | 'command_summary'
    | 'diff_summary'
    | 'link'
    | 'final_report'
    | 'error'
    | 'progress';
  title: string;
  summary: string | null;
  url: string | null;
  createdAt: string | null;
  raw: JsonRecord;
};

/** Dashboard rollup used by the overview page. */
export type DashboardSummary = {
  activeRuns: number;
  waitingForUser: number;
  failedOrBlocked: number;
  openTickets: number;
  repos: number;
  worktrees: number;
  recentArtifacts: SurfaceArtifact[];
  raw: JsonRecord;
};

/** Compact git status summary surfaced on repo/worktree cards. */
export type GitStatusSummary = {
  branch: string | null;
  dirty: boolean;
  filesChanged: number | null;
  insertions: number | null;
  deletions: number | null;
  untracked: number | null;
  staged: number | null;
  hasUpstream: boolean | null;
  ahead: number | null;
  behind: number | null;
};

/** Repository index row. */
export type RepoSummary = {
  id: string;
  name: string;
  path: string | null;
  status: WorkStatus;
  defaultBranch: string | null;
  worktreeCount: number;
  activeRuns: number;
  openTickets: number;
  lastActivityAt: string | null;
  gitStatus?: GitStatusSummary | null;
  raw: JsonRecord;
};

/** Worktree index/detail summary. */
export type WorktreeSummary = {
  id: string;
  repoId: string | null;
  name: string;
  path: string | null;
  branch: string | null;
  status: WorkStatus;
  activeRuns: number;
  openTickets: number;
  lastActivityAt: string | null;
  gitStatus?: GitStatusSummary | null;
  raw: JsonRecord;
};

/** Ticket queue row. */
export type TicketSummary = {
  id: string;
  number: number | null;
  title: string;
  status: WorkStatus;
  workspaceKind: 'repo' | 'worktree' | 'unscoped';
  workspaceId: string | null;
  workspacePath: string | null;
  repoId: string | null;
  worktreeId: string | null;
  path: string | null;
  ticketPath: string | null;
  agentId: string | null;
  chatKey: string | null;
  runId: string | null;
  updatedAt: string | null;
  durationSeconds: number | null;
  diffStats: {
    insertions: number;
    deletions: number;
    filesChanged: number;
  } | null;
  errors: string[];
  raw: JsonRecord;
};

/** Ticket detail shape with associated progress and artifacts. */
export type TicketDetail = TicketSummary & {
  body: string;
  progress: PmaRunProgress | null;
  artifacts: SurfaceArtifact[];
};

/** Contextspace document shape for editable durable workspace context. */
export type ContextspaceDocument = {
  id: string;
  name: string;
  kind: string;
  content: string;
  updatedAt: string | null;
  isPinned: boolean;
  raw: JsonRecord;
};

export function mapPmaChatSummary(raw: JsonRecord): PmaChatSummary {
  const latest = asRecord(raw.latest_execution ?? raw.latest_turn ?? raw.turn);
  const status = normalizeWorkStatus(
    raw.effective_status ??
      raw.execution_status ??
      raw.normalized_status ??
      raw.runtime_status ??
      raw.status ??
      latest.status ??
      raw.lifecycle_status
  );
  const id = stringValue(raw.thread_target_id ?? raw.managed_thread_id ?? raw.thread_id ?? raw.id, 'unknown-chat');
  const resourceKind = nullableString(raw.resource_kind);
  const resourceId = nullableString(raw.resource_id);
  const repoId = nullableString(raw.repo_id) ?? (resourceKind === 'repo' ? resourceId : null);
  const worktreeId =
    nullableString(raw.worktree_repo_id ?? raw.worktree_id) ?? (resourceKind === 'worktree' ? resourceId : null);
  const ticketId = ticketIdFromRaw(raw);
  const isTicketFlow = detectTicketFlowChat(raw, ticketId);
  return {
    id,
    title: readableThreadTitle(raw, id, ticketId),
    lifecycleStatus: nullableString(raw.lifecycle_status ?? raw.lifecycleStatus),
    status,
    agentId: nullableString(raw.agent_id ?? raw.agent),
    agentProfile: nullableString(raw.agent_profile ?? raw.agentProfile),
    model: nullableString(raw.model ?? latest.model),
    repoId,
    worktreeId,
    ticketId,
    ticketDone: booleanOrNull(raw.ticket_done ?? raw.ticketDone),
    ticketPath: nullableString(raw.ticket_path ?? raw.ticketPath),
    runId: nullableString(raw.run_id ?? raw.runId),
    flowType: nullableString(raw.flow_type ?? raw.flowType),
    isTicketFlow,
    progressPercent: numberOrNull(raw.progress_percent ?? raw.progress),
    updatedAt: dateString(
      raw.updated_at ??
        raw.last_activity_at ??
        raw.status_changed_at ??
        raw.created_at ??
        latest.last_event_at ??
        latest.finished_at ??
        latest.started_at
    ),
    raw
  };
}

export function mapPmaChatMessage(raw: JsonRecord): PmaChatMessage {
  const id = stringValue(raw.managed_turn_id ?? raw.turn_id ?? raw.message_id ?? raw.id, 'unknown-message');
  const role = normalizeRole(raw.role ?? raw.author ?? raw.request_kind);
  const text = normalizeMessageText(raw, role);
  return {
    id,
    chatId: nullableString(raw.managed_thread_id ?? raw.thread_id ?? raw.chat_id),
    role,
    text,
    createdAt: dateString(raw.created_at ?? raw.started_at ?? raw.timestamp),
    status: normalizeOptionalWorkStatus(raw.status),
    artifacts: asArray(raw.artifacts ?? raw.attachments).map(mapSurfaceArtifact),
    raw
  };
}

export function mapPmaTimelineItem(raw: JsonRecord): PmaTimelineItem {
  const payload = asRecord(raw.payload);
  return {
    id: stringValue(raw.item_id ?? raw.id, 'timeline-item'),
    kind: normalizeTimelineKind(raw.kind),
    orderKey: stringValue(raw.order_key ?? raw.item_id ?? raw.id, ''),
    timestamp: dateString(raw.timestamp),
    chatId: nullableString(raw.managed_thread_id ?? raw.thread_id ?? raw.chat_id),
    turnId: nullableString(raw.managed_turn_id ?? raw.turn_id),
    status: normalizeOptionalWorkStatus(raw.status),
    payload,
    raw
  };
}

export function mapPmaRunProgress(raw: JsonRecord): PmaRunProgress {
  const snapshot = asRecord(raw.snapshot ?? raw.progress);
  const source = Object.keys(snapshot).length ? { ...raw, ...snapshot } : raw;
  const id = stringValue(source.managed_turn_id ?? source.run_id ?? source.id, 'unknown-run');
  return {
    id,
    chatId: nullableString(source.managed_thread_id ?? source.thread_id),
    status: normalizeWorkStatus(source.turn_status ?? source.status ?? source.activity),
    workStatus: nullableString(source.work_status),
    operatorStatus: nullableString(source.operator_status),
    terminal: source.terminal === true,
    streamShouldClose: source.stream_should_close === true,
    streamCloseReason: nullableString(source.stream_close_reason),
    phase: nullableString(source.phase),
    guidance: nullableString(source.guidance),
    queueDepth: numberOrNull(source.queue_depth) ?? 0,
    elapsedSeconds: numberOrNull(source.elapsed_seconds ?? source.duration_seconds),
    idleSeconds: numberOrNull(source.idle_seconds),
    lastEventId: numberOrNull(source.last_event_id),
    lastEventAt: dateString(
      source.last_event_at ??
        source.last_activity_at ??
        source.effective_last_activity_at ??
        source.finished_at ??
        source.started_at ??
        source.created_at
    ),
    progressPercent: progressPercentFromRun(source),
    events: asArray(source.events ?? source.lifecycle_events).map(mapSurfaceArtifact),
    raw
  };
}

export function mapSurfaceArtifact(raw: JsonRecord): SurfaceArtifact {
  const progressItem = asRecord(raw.progress_item);
  const id = stringValue(
    raw.id ?? raw.artifact_id ?? raw.name ?? progressItem.item_id ?? raw.event_id ?? raw.rel_path,
    'artifact'
  );
  const title = stringValue(progressItem.title ?? raw.title ?? raw.name ?? raw.summary ?? raw.event_type ?? raw.item_type, id);
  return {
    id,
    kind: normalizeArtifactKind(raw.kind ?? raw.type ?? raw.item_type ?? raw.event_type ?? raw.name ?? raw.rel_path ?? raw.url),
    title,
    summary: nullableString(raw.summary ?? raw.description ?? raw.message),
    url: nullableString(raw.url ?? raw.href ?? raw.preview_url),
    createdAt: dateString(raw.created_at ?? raw.modified_at ?? raw.received_at),
    raw
  };
}

export function mapDashboardSummary(raw: JsonRecord): DashboardSummary {
  const items = asArray(raw.items);
  const threads = asArray(raw.managed_threads ?? raw.threads);
  const files = [...asArray(raw.pma_files_detail), ...asArray(asRecord(raw.pma_files_detail).inbox)];
  const activeRuns = countByStatus([...items, ...threads], ['running']);
  const waitingForUser = countByStatus([...items, ...threads], ['waiting']);
  const failedOrBlocked = countByStatus([...items, ...threads], ['failed', 'blocked']);
  return {
    activeRuns,
    waitingForUser,
    failedOrBlocked,
    openTickets: items.length,
    repos: numberOrNull(raw.repo_count ?? raw.repos) ?? 0,
    worktrees: numberOrNull(raw.worktree_count ?? raw.worktrees) ?? 0,
    recentArtifacts: files.map(mapSurfaceArtifact),
    raw
  };
}

export function mapRepoSummary(raw: JsonRecord): RepoSummary {
  const id = stringValue(raw.id ?? raw.repo_id ?? raw.name, 'unknown-repo');
  const ticketFlow = asRecord(raw.ticket_flow_display ?? raw.ticket_flow);
  const runState = asRecord(raw.run_state);
  const activeRuns = Boolean(ticketFlow.is_active ?? runState.is_active)
    ? 1
    : numberOrNull(raw.active_runs ?? raw.active_run_count) ?? 0;
  const totalTickets = numberOrNull(ticketFlow.total_count);
  const doneTickets = numberOrNull(ticketFlow.done_count);
  return {
    id,
    name: stringValue(raw.name ?? raw.display_name, id),
    path: nullableString(raw.path ?? raw.repo_root),
    status: normalizeResourceWorkStatus(ticketFlow.status ?? runState.flow_status ?? raw.normalized_status ?? raw.runtime_status ?? raw.status ?? raw.lifecycle_status),
    defaultBranch: nullableString(raw.default_branch ?? raw.branch ?? raw.current_branch),
    worktreeCount: numberOrNull(raw.worktree_count ?? raw.worktrees_count ?? asArray(raw.worktrees).length) ?? 0,
    activeRuns,
    openTickets:
      numberOrNull(raw.open_tickets ?? raw.open_ticket_count) ??
      (totalTickets !== null && doneTickets !== null ? Math.max(0, totalTickets - doneTickets) : 0),
    lastActivityAt: dateString(raw.last_activity_at ?? raw.updated_at ?? runState.last_event_at ?? raw.last_run_started_at),
    gitStatus: mapGitStatusSummary(raw),
    raw
  };
}

export function mapWorktreeSummary(raw: JsonRecord): WorktreeSummary {
  const id = stringValue(raw.worktree_id ?? raw.id ?? raw.repo_id ?? raw.name, 'unknown-worktree');
  const ticketFlow = asRecord(raw.ticket_flow_display ?? raw.ticket_flow);
  const runState = asRecord(raw.run_state);
  const activeRuns = Boolean(ticketFlow.is_active ?? runState.is_active)
    ? 1
    : numberOrNull(raw.active_runs ?? raw.active_run_count) ?? 0;
  const totalTickets = numberOrNull(ticketFlow.total_count);
  const doneTickets = numberOrNull(ticketFlow.done_count);
  return {
    id,
    repoId: nullableString(raw.worktree_of ?? raw.base_repo_id ?? raw.parent_repo_id),
    name: stringValue(raw.name ?? raw.display_name ?? raw.branch, id),
    path: nullableString(raw.path ?? raw.workspace_root),
    branch: nullableString(raw.branch ?? raw.current_branch),
    status: normalizeResourceWorkStatus(ticketFlow.status ?? runState.flow_status ?? raw.normalized_status ?? raw.runtime_status ?? raw.status ?? raw.lifecycle_status),
    activeRuns,
    openTickets:
      numberOrNull(raw.open_tickets ?? raw.open_ticket_count) ??
      (totalTickets !== null && doneTickets !== null ? Math.max(0, totalTickets - doneTickets) : 0),
    lastActivityAt: dateString(raw.last_activity_at ?? raw.updated_at ?? runState.last_event_at ?? raw.last_run_started_at),
    gitStatus: mapGitStatusSummary(raw),
    raw
  };
}

function mapGitStatusSummary(raw: JsonRecord): GitStatusSummary | null {
  const candidates = [raw.git_status, raw.git, asRecord(raw.run_state).git_status];
  const source = candidates.find(
    (value): value is JsonRecord => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
  );
  if (!source) return null;
  const upstream = asRecord(source.upstream ?? source);
  const diff = asRecord(source.diff_stats ?? source.diff ?? source);
  const filesChanged = numberOrNull(source.files_changed ?? diff.files_changed ?? diff.filesChanged);
  const insertions = numberOrNull(source.insertions ?? diff.insertions);
  const deletions = numberOrNull(source.deletions ?? diff.deletions);
  const untracked = numberOrNull(source.untracked ?? source.untracked_count);
  const staged = numberOrNull(source.staged ?? source.staged_count);
  const aheadVal = numberOrNull(source.ahead ?? upstream.ahead);
  const behindVal = numberOrNull(source.behind ?? upstream.behind);
  const hasUpstreamRaw = source.has_upstream ?? upstream.has_upstream;
  const hasUpstream = typeof hasUpstreamRaw === 'boolean' ? hasUpstreamRaw : aheadVal !== null || behindVal !== null ? true : null;
  const dirtyRaw = source.dirty ?? source.is_dirty;
  const dirty =
    typeof dirtyRaw === 'boolean'
      ? dirtyRaw
      : (filesChanged ?? 0) > 0 || (untracked ?? 0) > 0 || (staged ?? 0) > 0;
  const hasAnySignal =
    dirty ||
    filesChanged !== null ||
    insertions !== null ||
    deletions !== null ||
    untracked !== null ||
    staged !== null ||
    aheadVal !== null ||
    behindVal !== null ||
    hasUpstream !== null ||
    nullableString(source.branch) !== null;
  if (!hasAnySignal) return null;
  return {
    branch: nullableString(source.branch),
    dirty,
    filesChanged,
    insertions,
    deletions,
    untracked,
    staged,
    hasUpstream,
    ahead: aheadVal,
    behind: behindVal
  };
}

export function mapTicketSummary(raw: JsonRecord): TicketSummary {
  const frontmatter = asRecord(raw.frontmatter);
  const index = numberOrNull(raw.index);
  const path = nullableString(raw.ticket_path ?? raw.path);
  const resourceKind = nullableString(raw.workspace_kind ?? raw.resource_kind ?? frontmatter.resource_kind);
  const resourceId = nullableString(raw.resource_id ?? frontmatter.resource_id);
  const workspaceKind = resourceKind === 'repo' || resourceKind === 'worktree' ? resourceKind : 'unscoped';
  const workspaceId = nullableString(raw.workspace_id) ?? (workspaceKind === 'unscoped' ? null : resourceId);
  const id = stringValue(
    raw.id ?? frontmatter.ticket_id ?? raw.ticket_id ?? raw.current_ticket ?? path ?? raw.run_id ?? index,
    'unknown-ticket'
  );
  const statusSource = raw.status ?? raw.state ?? raw.canonical_status;
  const done = Boolean(frontmatter.done ?? raw.done);
  const errors = stringArray(raw.errors);
  const diffStats = asRecord(raw.diff_stats);
  return {
    id,
    number: index,
    title: stringValue(frontmatter.title ?? raw.title ?? raw.summary ?? raw.current_ticket_title, index ? `Ticket ${index}` : id),
    status: errors.length ? 'invalid' : done ? 'done' : normalizeWorkStatus(statusSource),
    workspaceKind,
    workspaceId,
    workspacePath: nullableString(raw.workspace_path),
    repoId: nullableString(raw.repo_id ?? frontmatter.repo_id) ?? (resourceKind === 'repo' ? resourceId : null),
    worktreeId:
      nullableString(raw.worktree_id ?? raw.worktree_repo_id ?? frontmatter.worktree_id ?? frontmatter.worktree_repo_id) ??
      (resourceKind === 'worktree' ? resourceId : null),
    path,
    ticketPath: path,
    agentId: nullableString(frontmatter.agent ?? raw.agent),
    chatKey: nullableString(raw.chat_key ?? frontmatter.chat_key),
    runId: nullableString(raw.run_id),
    updatedAt: dateString(raw.updated_at ?? raw.mtime ?? raw.created_at ?? raw.last_activity_at),
    durationSeconds: numberOrNull(raw.duration_seconds),
    diffStats:
      Object.keys(diffStats).length > 0
        ? {
            insertions: numberOrNull(diffStats.insertions) ?? 0,
            deletions: numberOrNull(diffStats.deletions) ?? 0,
            filesChanged: numberOrNull(diffStats.files_changed ?? diffStats.filesChanged) ?? 0
          }
        : null,
    errors,
    raw
  };
}

export function mapTicketDetail(raw: JsonRecord): TicketDetail {
  const summary = mapTicketSummary(raw);
  const history = asArray(raw.history);
  return {
    ...summary,
    body: stringValue(raw.body ?? raw.content ?? raw.markdown, ''),
    progress: raw.run || raw.status ? mapPmaRunProgress(asRecord(raw.run ?? raw.status ?? raw)) : null,
    artifacts: history.flatMap((entry) => asArray(entry.attachments)).map(mapSurfaceArtifact)
  };
}

export function mapContextspaceDocument(raw: JsonRecord): ContextspaceDocument {
  const kind = stringValue(raw.kind ?? raw.name ?? raw.path, 'document');
  const name = stringValue(raw.name ?? raw.path ?? kind, kind);
  return {
    id: stringValue(raw.id ?? raw.path ?? kind, kind),
    name,
    kind,
    content: stringValue(raw.content ?? raw.text, ''),
    updatedAt: dateString(raw.updated_at ?? raw.modified_at ?? raw.mtime),
    isPinned: Boolean(raw.is_pinned ?? raw.pinned),
    raw
  };
}

export function normalizeWorkStatus(value: unknown): WorkStatus {
  const text = String(value ?? '').trim().toLowerCase();
  if (['running', 'active', 'in_progress', 'progress'].includes(text)) return 'running';
  if (['waiting', 'paused', 'needs_user', 'queued', 'pending'].includes(text)) return 'waiting';
  if (['ok', 'done', 'complete', 'completed', 'interrupted', 'cancelled', 'canceled', 'aborted'].includes(text)) return 'done';
  if (text === 'idle') return 'idle';
  if (['failed', 'error', 'errored'].includes(text)) return 'failed';
  if (['blocked', 'stalled'].includes(text)) return 'blocked';
  if (['invalid', 'malformed', 'validation_failed', 'needs_repair'].includes(text)) return 'invalid';
  return 'idle';
}

function booleanOrNull(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  return null;
}

function normalizeResourceWorkStatus(value: unknown): WorkStatus {
  const text = String(value ?? '').trim().toLowerCase();
  // Repo/worktree cards should only say "waiting" for true attention states.
  // Flow "pending" and PMA queue "queued" still represent active work, not user input.
  if (['pending', 'queued', 'stopping'].includes(text)) return 'running';
  return normalizeWorkStatus(value);
}

export function normalizeOptionalWorkStatus(value: unknown): WorkStatus | null {
  if (value === undefined || value === null) return null;
  if (typeof value === 'string' && !value.trim()) return null;
  return normalizeWorkStatus(value);
}

function normalizeRole(value: unknown): PmaChatMessage['role'] {
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'user') return 'user';
  if (['assistant', 'pma', 'agent'].includes(text)) return 'assistant';
  if (text === 'tool') return 'tool';
  return 'system';
}

function normalizeTimelineKind(value: unknown): PmaTimelineItemKind {
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'user_message') return 'user_message';
  if (text === 'assistant_message') return 'assistant_message';
  if (text === 'intermediate') return 'intermediate';
  if (text === 'tool_group') return 'tool_group';
  if (text === 'status') return 'status';
  if (text === 'approval') return 'approval';
  if (text === 'artifact') return 'artifact';
  if (text === 'delivery_state') return 'delivery_state';
  if (text === 'lifecycle') return 'lifecycle';
  return 'intermediate';
}

function normalizeMessageText(raw: JsonRecord, role: PmaChatMessage['role']): string {
  if (role === 'user') {
    const text = firstText(raw.text, raw.content, raw.message, raw.delivered_message, raw.prompt, raw.prompt_text, raw.prompt_preview);
    return isCarTicketFlowControlPrompt(text) ? '' : text;
  }
  if (role === 'assistant') {
    return firstText(
      raw.text,
      raw.content,
      raw.assistant_text,
      raw.output_text,
      raw.final_response,
      raw.response_text,
      raw.final_message,
      raw.final_output,
      raw.final_report,
      raw.latest_assistant_text,
      raw.assistant_preview,
      raw.summary,
      raw.error
    );
  }
  return firstText(raw.text, raw.content, raw.summary, raw.message);
}

function readableThreadTitle(raw: JsonRecord, fallback: string, ticketId: string | null): string {
  const explicit = stringValue(raw.display_name ?? raw.name ?? raw.title, fallback);
  const chatDisplayName = nullableString(raw.chat_display_name);
  if (chatDisplayName && isChatSurfaceIdTitle(explicit, raw)) return chatDisplayName;
  if (
    !isGenericChatTitle(explicit) &&
    !isGenericTicketFlowTitle(explicit) &&
    !isCarTicketFlowControlPrompt(explicit)
  ) {
    return explicit;
  }

  const firstMessageExcerpt = firstUserMessageExcerpt(raw);
  if (firstMessageExcerpt) return firstMessageExcerpt;

  const workspace = workspaceLabel(raw.workspace_root);
  const repo = nullableString(raw.repo_id);
  const resourceKind = nullableString(raw.resource_kind);
  const resourceId = nullableString(raw.resource_id);
  const resource =
    resourceKind === 'worktree'
      ? `worktree ${resourceId ?? workspace ?? repo ?? 'workspace'}`
      : repo ?? resourceId ?? workspace;

  if (isGenericChatTitle(explicit) && !ticketId && !isCarTicketFlowControlPrompt(explicit)) {
    return resource ? `Chat · ${resource}` : explicit;
  }

  const parts = ['Ticket flow'];
  if (ticketId) parts.push(ticketId);
  if (resource) parts.push(resource);
  return parts.join(' · ');
}

function isGenericChatTitle(value: string): boolean {
  const text = value.trim().toLowerCase();
  return text === 'new pma chat' || text === 'new chat' || text === 'untitled chat' || text === '';
}

function isChatSurfaceIdTitle(value: string, raw: JsonRecord): boolean {
  const text = value.trim().toLowerCase();
  if (!/^(discord|telegram):\S+$/.test(text)) return false;
  const bindingKind = nullableString(raw.binding_kind)?.toLowerCase();
  if (bindingKind === 'discord' || bindingKind === 'telegram') return true;
  return raw.chat_bound === true;
}

function firstUserMessageExcerpt(raw: JsonRecord): string | null {
  const candidate = firstText(
    raw.first_message_excerpt,
    raw.first_user_message,
    raw.last_user_message,
    raw.last_message_preview,
    raw.prompt_preview
  );
  if (!candidate) return null;
  const trimmed = candidate.trim();
  if (!trimmed) return null;
  if (isCarTicketFlowControlPrompt(trimmed)) return null;
  const oneLine = trimmed.split(/\r?\n/)[0]?.trim() ?? '';
  if (!oneLine) return null;
  const truncated = oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine;
  return truncated;
}

function isGenericTicketFlowTitle(value: string): boolean {
  return /^ticket-flow(?::[\w.-]+)?$/i.test(value.trim());
}

function isCarTicketFlowControlPrompt(value: string): boolean {
  const text = value.trim();
  return text.startsWith('<CAR_TICKET_FLOW_PROMPT') || text.includes('<CAR_CURRENT_TICKET_FILE>');
}

function detectTicketFlowChat(raw: JsonRecord, ticketId: string | null): boolean {
  if (ticketId) return true;
  const name = nullableString(raw.name ?? raw.display_name ?? raw.title);
  if (name && isGenericTicketFlowTitle(name)) return true;
  const flowKind = nullableString(raw.flow_type ?? raw.flow_kind ?? raw.flow ?? raw.kind ?? raw.thread_kind);
  if (flowKind && /ticket[-_]?flow/i.test(flowKind)) return true;
  const prompt = firstText(raw.prompt_preview, raw.prompt, raw.prompt_text, raw.last_message_preview);
  if (prompt && isCarTicketFlowControlPrompt(prompt)) return true;
  return false;
}

function ticketIdFromRaw(raw: JsonRecord): string | null {
  const direct = nullableString(raw.ticket_id ?? raw.current_ticket_id ?? raw.current_ticket);
  const text = firstText(
    raw.last_message_preview,
    raw.prompt_preview,
    raw.prompt,
    raw.prompt_text,
    raw.message,
    raw.name,
    raw.title
  );
  return extractTicketId(text) ?? direct;
}

function extractTicketId(value: string): string | null {
  const ticketNumber = value.match(/\bTICKET-\d+[A-Za-z0-9_-]*\b/);
  if (ticketNumber) return ticketNumber[0];
  const frontmatterId = value.match(/\bticket_id:\s*["']?([A-Za-z0-9_.:-]+)["']?/);
  return frontmatterId?.[1] ?? null;
}

function workspaceLabel(value: unknown): string | null {
  const text = nullableString(value);
  if (!text) return null;
  const parts = text.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) ?? text;
}

function normalizeArtifactKind(value: unknown): SurfaceArtifact['kind'] {
  const text = String(value ?? '').trim().toLowerCase();
  if (text.includes('screenshot')) return 'screenshot';
  if (text.match(/\.(png|jpe?g|gif|webp|avif)$/) || text.includes('image')) return 'image';
  if (text.includes('preview') || text.includes('preview_url')) return 'preview_url';
  if (text.includes('test')) return 'test_result';
  if (text.includes('command') || text.includes('cmd')) return 'command_summary';
  if (text.includes('diff')) return 'diff_summary';
  if (text.includes('report') || text.includes('final')) return 'final_report';
  if (text.includes('error') || text.includes('failed')) return 'error';
  if (text.includes('progress') || text.includes('turn_') || text.includes('tool_')) return 'progress';
  if (text === 'link') return 'link';
  if (text.startsWith('http') || text.includes('pull request') || text.includes('pr/') || text.includes('github')) return 'link';
  return 'file';
}

function countByStatus(items: JsonRecord[], statuses: WorkStatus[]): number {
  return items.filter((item) =>
    statuses.includes(normalizeWorkStatus(item.normalized_status ?? item.runtime_status ?? item.status ?? item.state ?? item.turn_status ?? item.lifecycle_status))
  ).length;
}

function progressPercentFromRun(source: JsonRecord): number | null {
  const explicit = numberOrNull(source.progress_percent ?? source.progress);
  if (explicit !== null) return clampPercent(explicit);
  const ticketProgress = asRecord(source.ticket_progress);
  const total = numberOrNull(ticketProgress.total);
  const done = numberOrNull(ticketProgress.done);
  if (total === null || done === null || total <= 0) return null;
  return clampPercent((done / total) * 100);
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asArray(value: unknown): JsonRecord[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => (typeof item === 'string' ? { summary: item } : item)).filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object');
}

function stringValue(value: unknown, fallback: string): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return fallback;
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function dateString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return new Date(value * 1000).toISOString();
  return null;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}
