import { mapResult, pmaApi, type ApiResult, type PmaApiClient } from '$lib/api/client';
import {
  mapReadModelContract,
  READ_MODEL_CONTRACT_VERSION,
  type ChatDetailSnapshot,
  type ChatIndexCounters,
  type ChatIndexRow,
  type ChatIndexSnapshot,
  type ChatTimelineItem,
  type ChatThreadProjection,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import { legacyChatIndexRecordToChatIndexRow } from './readModelViewModels';

type JsonRecord = Record<string, unknown>;

export type ChatIndexRequest = {
  filter?: ChatIndexSnapshot['filter'];
  query?: string | null;
  cursor?: string | null;
  limit?: number;
};

export type ReadModelSnapshotClient = {
  chatIndex(request?: ChatIndexRequest): Promise<ApiResult<ChatIndexSnapshot>>;
  chatDetail(chatId: string, timelineLimit?: number): Promise<ApiResult<ChatDetailSnapshot>>;
  repoWorktreeTopology(kind?: 'all' | 'repo' | 'worktree', limit?: number, cursor?: string | null): Promise<ApiResult<RepoWorktreeTopologySnapshot>>;
  repoWorktreeRuntime(kind?: 'all' | 'repo' | 'worktree', limit?: number, cursor?: string | null): Promise<ApiResult<RepoWorktreeRuntimeSnapshot>>;
  repoDetail(repoId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
  worktreeDetail(worktreeId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
  ticketDetail(ticketId: string, owner: { kind: 'repo' | 'worktree'; id: string }): Promise<ApiResult<TicketDetailSnapshot>>;
};

export function createReadModelSnapshotClient(api: PmaApiClient = pmaApi): ReadModelSnapshotClient {
  return {
    chatIndex: async (request = {}) => {
      const params = new URLSearchParams({
        view: legacyChatIndexView(request.filter ?? 'all'),
        limit: String(request.limit ?? 50)
      });
      if (request.query) params.set('search', request.query);
      if (request.cursor) params.set('offset', request.cursor);
      return mapResult(await api.getJson<JsonRecord>(`/hub/chat/index?${params.toString()}`), (payload) =>
        legacyChatIndexSnapshotToContract(payload, request)
      );
    },
    chatDetail: async (chatId, timelineLimit = 50) => {
      const params = new URLSearchParams({ timeline_limit: String(timelineLimit) });
      return mapResult(await api.getJson<JsonRecord>(`/hub/chat/threads/${encodeURIComponent(chatId)}/detail?${params.toString()}`), (payload) =>
        legacyChatDetailSnapshotToContract(payload, chatId, timelineLimit)
      );
    },
    repoWorktreeTopology: (kind, limit, cursor) => api.readModels.repoWorktreeTopology(kind, limit, cursor),
    repoWorktreeRuntime: (kind, limit, cursor) => api.readModels.repoWorktreeRuntime(kind, limit, cursor),
    repoDetail: (repoId) => api.readModels.repoDetail(repoId),
    worktreeDetail: (worktreeId) => api.readModels.worktreeDetail(worktreeId),
    ticketDetail: async (ticketId, owner) =>
      mapResult(await api.readModels.ticketDetail(ticketId, owner), (payload) => mapReadModelContract<TicketDetailSnapshot>(payload))
  };
}

export const readModelSnapshotClient = createReadModelSnapshotClient();

function legacyChatIndexSnapshotToContract(payload: JsonRecord, request: ChatIndexRequest): ChatIndexSnapshot {
  const rows = asRecords(payload.rows).map(legacyChatIndexRecordToChatIndexRow);
  const window = asRecord(payload.window);
  const limit = numberValue(window.limit, request.limit ?? 50);
  const offset = numberValue(window.offset, numberValue(request.cursor, 0));
  const total = numberValue(window.total_count ?? window.totalCount, rows.length);
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.index.snapshot',
    cursor: projectionCursor(payload.cursor, 'chat.index'),
    window: {
      limit,
      nextCursor: window.has_more === true ? String(offset + rows.length) : null,
      previousCursor: offset > 0 ? String(Math.max(0, offset - limit)) : null,
      totalEstimate: total,
      totalIsExact: true
    },
    filter: request.filter ?? 'all',
    query: request.query ?? null,
    rows,
    groups: [],
    counters: countersFromRows(rows, total),
    repair: repairPolicy('/hub/chat/index')
  };
}

function legacyChatDetailSnapshotToContract(payload: JsonRecord, chatId: string, timelineLimit: number): ChatDetailSnapshot {
  const threadRow = legacyChatIndexRecordToChatIndexRow(asRecord(payload.thread));
  const timeline = asRecord(payload.timeline);
  const timelineWindow = asRecord(timeline.window);
  const items = asRecords(timeline.items).map(legacyTimelineItemToContract);
  const queueSummary = asRecord(payload.queue_summary ?? payload.queueSummary);
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.detail.snapshot',
    cursor: projectionCursor(payload.cursor, 'chat.detail'),
    thread: chatThreadFromIndexRow(threadRow),
    timelineWindow: {
      limit: numberValue(timelineWindow.limit, timelineLimit),
      nextCursor: null,
      previousCursor: timelineWindow.has_older === true ? stringValue(timelineWindow.oldest_order_key) : null,
      totalEstimate: numberValue(timeline.item_count ?? timeline.itemCount, items.length),
      totalIsExact: true
    },
    timeline: items,
    queue: {
      depth: numberValue(queueSummary.depth, 0),
      activeTurnId: stringValue(queueSummary.active_turn_id ?? queueSummary.activeTurnId),
      queuedTurnIds: asRecords(queueSummary.items).map((item) => stringValue(item.managed_turn_id ?? item.turn_id)).filter(isString)
    },
    artifacts: [],
    repair: repairPolicy(`/hub/chat/threads/${encodeURIComponent(chatId)}/detail`)
  };
}

function chatThreadFromIndexRow(row: ChatIndexRow): ChatThreadProjection {
  return {
    chatId: row.chatId,
    surface: row.surface,
    title: row.title,
    status: row.status,
    repoId: row.repoId ?? null,
    worktreeId: row.worktreeId ?? null,
    ticketId: row.ticketId ?? null,
    runId: row.runId ?? null,
    agent: row.agent ?? null,
    agentProfile: row.agentProfile ?? null,
    chatKind: row.chatKind ?? null,
    model: row.model ?? null,
    archived: row.status === 'archived'
  };
}

function legacyTimelineItemToContract(raw: JsonRecord): ChatTimelineItem {
  const v2Identity = asRecord(raw.identity);
  const v2Provenance = asRecord(raw.provenance);
  const identity = v2Identity.timeline_item_id
    ? {
        timelineItemId: stringValue(v2Identity.timeline_item_id) ?? 'timeline-item',
        progressItemIds: asStrings(v2Identity.progress_item_ids),
        correlationId: stringValue(v2Identity.correlation_id)
      }
    : undefined;
  const provenance = v2Provenance.source_event_ids
    ? {
        sourceEventIds: Array.isArray(v2Provenance.source_event_ids) ? v2Provenance.source_event_ids as unknown[] : [],
        progressEventIds: Array.isArray(v2Provenance.progress_event_ids) ? v2Provenance.progress_event_ids as unknown[] : [],
        cursorEventId: stringValue(v2Provenance.cursor_event_id)
      }
    : undefined;
  return {
    itemId: stringValue(raw.item_id ?? raw.id, 'timeline-item') ?? 'timeline-item',
    kind: timelineKind(raw.kind),
    role: timelineRole(raw.role),
    createdAt: stringValue(raw.timestamp ?? raw.created_at, new Date(0).toISOString()) ?? new Date(0).toISOString(),
    text: stringValue(raw.text ?? raw.summary ?? raw.payload_text),
    artifactIds: [],
    clientMessageId: stringValue(raw.client_message_id ?? raw.clientMessageId),
    backendMessageId: stringValue(raw.backend_message_id ?? raw.managed_turn_id ?? raw.turn_id),
    ...(identity ? { identity } : {}),
    ...(provenance ? { provenance } : {})
  };
}

function projectionCursor(cursor: unknown, source: string): ProjectionCursor {
  const sequence = numberValue(cursor, Date.now());
  return {
    value: `${source}:${sequence}`,
    sequence,
    source,
    issuedAt: new Date().toISOString()
  };
}

function repairPolicy(snapshotRoute: string) {
  return {
    snapshotRoute,
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}

function countersFromRows(rows: ChatIndexRow[], total: number): ChatIndexCounters {
  return {
    total,
    waiting: rows.filter((row) => row.status === 'waiting').length,
    running: rows.filter((row) => row.status === 'running').length,
    unread: rows.reduce((sum, row) => sum + row.unreadCount, 0),
    archived: rows.filter((row) => row.status === 'archived').length
  };
}

function timelineKind(value: unknown): ChatTimelineItem['kind'] {
  if (value === 'assistant_message' || value === 'assistant') return 'assistant_message';
  if (value === 'user_message' || value === 'user') return 'user_message';
  if (value === 'tool_event' || value === 'tool') return 'tool_event';
  if (value === 'progress' || value === 'artifact' || value === 'system') return value;
  return 'system';
}

function timelineRole(value: unknown): ChatTimelineItem['role'] {
  if (value === 'user' || value === 'assistant' || value === 'tool' || value === 'system') return value;
  return null;
}

function legacyChatIndexView(filter: ChatIndexSnapshot['filter']): string {
  return filter === 'ticket_runs' ? 'ticket_run' : filter;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asRecords(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : [];
}

function stringValue(value: unknown, fallback: string | null = null): string | null {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function isString(value: string | null): value is string {
  return typeof value === 'string';
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}
