type JsonRecord = Record<string, unknown>;

export type WorkStatus = 'running' | 'waiting' | 'idle' | 'done' | 'failed' | 'blocked';

/** Stable chat-list shape consumed by PMA room components. */
export type PmaChatSummary = {
  id: string;
  title: string;
  status: WorkStatus;
  agentId: string | null;
  model: string | null;
  repoId: string | null;
  worktreeId: string | null;
  ticketId: string | null;
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

/** Compact run/tail/status state for chat, dashboard, repo, and ticket surfaces. */
export type PmaRunProgress = {
  id: string;
  chatId: string | null;
  status: WorkStatus;
  phase: string | null;
  guidance: string | null;
  queueDepth: number;
  elapsedSeconds: number | null;
  idleSeconds: number | null;
  lastEventId: number | null;
  lastEventAt: string | null;
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
  raw: JsonRecord;
};

/** Ticket queue row. */
export type TicketSummary = {
  id: string;
  title: string;
  status: WorkStatus;
  repoId: string | null;
  runId: string | null;
  updatedAt: string | null;
  raw: JsonRecord;
};

/** Ticket detail shape with associated progress and artifacts. */
export type TicketDetail = TicketSummary & {
  body: string;
  progress: PmaRunProgress | null;
  artifacts: SurfaceArtifact[];
};

/** Contextspace document shape for editable workspace memory. */
export type ContextspaceDocument = {
  id: string;
  name: string;
  kind: string;
  content: string;
  updatedAt: string | null;
  isPinned: boolean;
  raw: JsonRecord;
};

/** Sensitive CAR operation prompt. Normal coding-agent work should not create these. */
export type SensitiveApprovalRequest = {
  id: string;
  title: string;
  description: string;
  risk: 'low' | 'medium' | 'high';
  action: string;
  createdAt: string | null;
  raw: JsonRecord;
};

export function mapPmaChatSummary(raw: JsonRecord): PmaChatSummary {
  const latest = asRecord(raw.latest_execution ?? raw.latest_turn ?? raw.turn);
  const status = normalizeStatus(raw.lifecycle_status ?? raw.runtime_status ?? raw.status ?? latest.status);
  const id = stringValue(raw.thread_target_id ?? raw.managed_thread_id ?? raw.thread_id ?? raw.id, 'unknown-chat');
  return {
    id,
    title: stringValue(raw.display_name ?? raw.name ?? raw.title, id),
    status,
    agentId: nullableString(raw.agent_id ?? raw.agent),
    model: nullableString(raw.model ?? latest.model),
    repoId: nullableString(raw.repo_id ?? raw.resource_id),
    worktreeId: nullableString(raw.worktree_repo_id ?? raw.worktree_id),
    ticketId: nullableString(raw.ticket_id ?? raw.current_ticket_id),
    progressPercent: numberOrNull(raw.progress_percent ?? raw.progress),
    updatedAt: dateString(raw.updated_at ?? raw.last_activity_at ?? latest.finished_at ?? latest.started_at),
    raw
  };
}

export function mapPmaChatMessage(raw: JsonRecord): PmaChatMessage {
  const id = stringValue(raw.managed_turn_id ?? raw.turn_id ?? raw.message_id ?? raw.id, 'unknown-message');
  const text = stringValue(raw.text ?? raw.content ?? raw.prompt ?? raw.summary, '');
  return {
    id,
    chatId: nullableString(raw.managed_thread_id ?? raw.thread_id ?? raw.chat_id),
    role: normalizeRole(raw.role ?? raw.author ?? raw.request_kind),
    text,
    createdAt: dateString(raw.created_at ?? raw.started_at ?? raw.timestamp),
    status: raw.status === undefined ? null : normalizeStatus(raw.status),
    artifacts: asArray(raw.artifacts ?? raw.attachments).map(mapSurfaceArtifact),
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
    status: normalizeStatus(source.turn_status ?? source.status ?? source.activity),
    phase: nullableString(source.phase),
    guidance: nullableString(source.guidance),
    queueDepth: numberOrNull(source.queue_depth) ?? 0,
    elapsedSeconds: numberOrNull(source.elapsed_seconds),
    idleSeconds: numberOrNull(source.idle_seconds),
    lastEventId: numberOrNull(source.last_event_id),
    lastEventAt: dateString(source.last_event_at ?? source.last_activity_at),
    events: asArray(source.events ?? source.lifecycle_events).map(mapSurfaceArtifact),
    raw
  };
}

export function mapSurfaceArtifact(raw: JsonRecord): SurfaceArtifact {
  const id = stringValue(raw.id ?? raw.artifact_id ?? raw.name ?? raw.event_id ?? raw.rel_path, 'artifact');
  const title = stringValue(raw.title ?? raw.name ?? raw.summary ?? raw.event_type ?? raw.item_type, id);
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
  const threads = asArray(raw.pma_threads ?? raw.threads);
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
  return {
    id,
    name: stringValue(raw.name ?? raw.display_name, id),
    path: nullableString(raw.path ?? raw.repo_root),
    status: normalizeStatus(raw.status ?? raw.runtime_status),
    defaultBranch: nullableString(raw.default_branch ?? raw.branch),
    worktreeCount: numberOrNull(raw.worktree_count ?? raw.worktrees_count ?? asArray(raw.worktrees).length) ?? 0,
    activeRuns: numberOrNull(raw.active_runs ?? raw.active_run_count) ?? 0,
    openTickets: numberOrNull(raw.open_tickets ?? raw.open_ticket_count) ?? 0,
    lastActivityAt: dateString(raw.last_activity_at ?? raw.updated_at),
    raw
  };
}

export function mapWorktreeSummary(raw: JsonRecord): WorktreeSummary {
  const id = stringValue(raw.worktree_id ?? raw.id ?? raw.repo_id ?? raw.name, 'unknown-worktree');
  return {
    id,
    repoId: nullableString(raw.base_repo_id ?? raw.repo_id ?? raw.parent_repo_id),
    name: stringValue(raw.name ?? raw.display_name ?? raw.branch, id),
    path: nullableString(raw.path ?? raw.workspace_root),
    branch: nullableString(raw.branch ?? raw.current_branch),
    status: normalizeStatus(raw.status ?? raw.runtime_status),
    activeRuns: numberOrNull(raw.active_runs ?? raw.active_run_count) ?? 0,
    openTickets: numberOrNull(raw.open_tickets ?? raw.open_ticket_count) ?? 0,
    lastActivityAt: dateString(raw.last_activity_at ?? raw.updated_at),
    raw
  };
}

export function mapTicketSummary(raw: JsonRecord): TicketSummary {
  const frontmatter = asRecord(raw.frontmatter);
  const id = stringValue(
    frontmatter.ticket_id ?? raw.ticket_id ?? raw.id ?? raw.current_ticket ?? raw.path ?? raw.run_id ?? raw.index,
    'unknown-ticket'
  );
  return {
    id,
    title: stringValue(frontmatter.title ?? raw.title ?? raw.summary ?? raw.current_ticket_title, id),
    status: Boolean(frontmatter.done ?? raw.done) ? 'done' : normalizeStatus(raw.status ?? raw.state ?? raw.canonical_status),
    repoId: nullableString(raw.repo_id),
    runId: nullableString(raw.run_id),
    updatedAt: dateString(raw.updated_at ?? raw.created_at ?? raw.last_activity_at),
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
    updatedAt: dateString(raw.updated_at ?? raw.modified_at),
    isPinned: Boolean(raw.is_pinned ?? raw.pinned),
    raw
  };
}

export function mapSensitiveApprovalRequest(raw: JsonRecord): SensitiveApprovalRequest {
  const id = stringValue(raw.id ?? raw.approval_id ?? raw.action_queue_id ?? raw.run_id, 'approval');
  const action = stringValue(raw.action ?? raw.operation ?? raw.item_type, 'review');
  return {
    id,
    title: stringValue(raw.title ?? raw.summary ?? action, action),
    description: stringValue(raw.description ?? raw.reason ?? raw.message, ''),
    risk: normalizeRisk(raw.risk ?? raw.sensitivity),
    action,
    createdAt: dateString(raw.created_at ?? raw.enqueued_at ?? raw.updated_at),
    raw
  };
}

function normalizeStatus(value: unknown): WorkStatus {
  const text = String(value ?? '').trim().toLowerCase();
  if (['running', 'active', 'in_progress', 'progress'].includes(text)) return 'running';
  if (['waiting', 'paused', 'needs_user', 'queued', 'pending'].includes(text)) return 'waiting';
  if (['ok', 'done', 'complete', 'completed', 'idle'].includes(text)) return text === 'idle' ? 'idle' : 'done';
  if (['failed', 'error', 'errored'].includes(text)) return 'failed';
  if (['blocked', 'stalled'].includes(text)) return 'blocked';
  return 'idle';
}

function normalizeRole(value: unknown): PmaChatMessage['role'] {
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'user') return 'user';
  if (['assistant', 'pma', 'agent'].includes(text)) return 'assistant';
  if (text === 'tool') return 'tool';
  return 'system';
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
  if (text.startsWith('http') || text.includes('pull request') || text.includes('pr/') || text.includes('github')) return 'link';
  return 'file';
}

function normalizeRisk(value: unknown): SensitiveApprovalRequest['risk'] {
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'high') return 'high';
  if (text === 'medium') return 'medium';
  return 'low';
}

function countByStatus(items: JsonRecord[], statuses: WorkStatus[]): number {
  return items.filter((item) => statuses.includes(normalizeStatus(item.status ?? item.state ?? item.runtime_status))).length;
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

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function dateString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}
