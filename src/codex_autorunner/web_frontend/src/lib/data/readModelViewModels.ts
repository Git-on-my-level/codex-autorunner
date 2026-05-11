import type { ChatIndexCounters, ChatIndexRow, ProjectionCursor, RuntimeProjection } from '$lib/api/readModelContracts';
import {
  buildTicketListViewModel,
  type SurfaceActionManifest,
  type TicketListViewModel,
  type TicketOwnerScope
} from '$lib/viewModels/ticket';
import {
  mapRepoSummary,
  mapWorktreeSummary,
  normalizeWorkStatus,
  type PmaChatSummary,
  type PmaRunProgress,
  type PmaTimelineItem,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketSummary,
  type WorktreeSummary,
  type WorkStatus
} from '$lib/viewModels/domain';
import type { PmaQueuedTurn } from '$lib/api/client';
import type { ReadModelEntityState } from './readModelStore';

type JsonRecord = Record<string, unknown>;

export function syntheticProjectionCursor(source: string, sequence = Date.now()): ProjectionCursor {
  return {
    value: `${source}:${sequence}`,
    sequence,
    source,
    issuedAt: new Date().toISOString()
  };
}

export function pmaChatSummaryToChatIndexRow(chat: PmaChatSummary): ChatIndexRow {
  return {
    chatId: chat.id,
    surface: surfaceFromRaw(chat.raw),
    title: chat.title,
    status: chatIndexStatus(chat),
    unreadCount: 0,
    lastActivityAt: chat.updatedAt,
    repoId: chat.repoId,
    worktreeId: chat.worktreeId,
    ticketId: chat.ticketId,
    runId: chat.runId ?? null,
    agent: chat.agentId,
    model: chat.model,
    groupId: chat.ticketId ? `ticket:${chat.ticketId}` : chat.runId ? `run:${chat.runId}` : null
  };
}

export function legacyChatIndexRecordToChatIndexRow(raw: JsonRecord): ChatIndexRow {
  const managedThreadId = stringValue(raw.managed_thread_id ?? raw.thread_target_id);
  const rowId = stringValue(raw.row_id, 'unknown-chat-row');
  const chatId = managedThreadId ?? rowId ?? 'unknown-chat-row';
  const resourceKind = stringValue(raw.resource_kind);
  const resourceId = stringValue(raw.resource_id);
  const queueDepth = numberValue(raw.queue_depth);
  const lifecycle = stringValue(raw.lifecycle)?.toLowerCase() ?? '';
  const lifecycleStatus = stringValue(raw.lifecycle_status)?.toLowerCase() ?? '';
  const runtimeStatus = stringValue(raw.runtime_status ?? raw.target_runtime_status)?.toLowerCase() ?? '';
  return {
    chatId,
    surface: surfaceFromKinds(raw.surface_kinds, raw.surface),
    title: stringValue(raw.title ?? raw.display_name, chatId) ?? chatId,
    status: legacyChatIndexStatus(lifecycle, lifecycleStatus, runtimeStatus, queueDepth),
    unreadCount: raw.unread === true ? 1 : 0,
    lastActivityAt: stringValue(raw.updated_at ?? raw.created_at),
    repoId: stringValue(raw.repo_id),
    worktreeId: stringValue(raw.worktree_id ?? raw.worktree_repo_id),
    ticketId: resourceKind === 'ticket' ? resourceId : stringValue(raw.ticket_id ?? raw.current_ticket_id),
    runId: resourceKind === 'run' || resourceKind === 'ticket_run' ? resourceId : stringValue(raw.run_id),
    agent: stringValue(raw.agent ?? raw.agent_id),
    model: stringValue(raw.model),
    groupId: stringValue(raw.group_id)
  };
}

export function chatIndexRowToPmaChatSummary(row: ChatIndexRow): PmaChatSummary {
  const raw: JsonRecord = {
    row,
    id: row.chatId,
    managed_thread_id: row.chatId,
    title: row.title,
    display_name: row.title,
    normalized_status: row.status,
    runtime_status: row.status,
    status: row.status,
    lifecycle_status: row.status === 'archived' ? 'archived' : 'active',
    repo_id: row.repoId,
    worktree_id: row.worktreeId,
    current_ticket_id: row.ticketId,
    ticket_id: row.ticketId,
    run_id: row.runId,
    agent_id: row.agent,
    model: row.model,
    last_activity_at: row.lastActivityAt,
    surface_kind: row.surface
  };
  return {
    id: row.chatId,
    title: row.title,
    lifecycleStatus: row.status === 'archived' ? 'archived' : 'active',
    status: normalizeWorkStatus(row.status),
    agentId: row.agent ?? null,
    agentProfile: null,
    model: row.model ?? null,
    repoId: row.repoId ?? null,
    worktreeId: row.worktreeId ?? null,
    ticketId: row.ticketId ?? null,
    runId: row.runId ?? null,
    flowType: null,
    isTicketFlow: Boolean(row.ticketId || row.runId || row.groupId?.startsWith('ticket') || row.groupId?.startsWith('run')),
    progressPercent: null,
    updatedAt: row.lastActivityAt ?? null,
    raw
  };
}

export function selectPmaChats(state: ReadModelEntityState): PmaChatSummary[] {
  return state.chatOrder.map((id) => state.chats[id]).filter(Boolean).map(chatIndexRowToPmaChatSummary);
}

export function selectPmaTimeline(state: ReadModelEntityState, chatId: string | null): PmaTimelineItem[] {
  if (!chatId) return [];
  const timeline = state.pmaTimelines[chatId];
  return timeline ? timeline.order.map((id) => timeline.itemsById[id]).filter(Boolean) : [];
}

export function selectPmaProgress(state: ReadModelEntityState, chatId: string | null): PmaRunProgress | null {
  return chatId ? state.pmaProgress[chatId] ?? null : null;
}

export function selectPmaQueue(state: ReadModelEntityState, chatId: string | null): PmaQueuedTurn[] {
  return chatId ? state.pmaQueues[chatId] ?? [] : [];
}

export function selectPmaArtifacts(state: ReadModelEntityState, chatId: string | null): SurfaceArtifact[] {
  return chatId ? state.pmaArtifacts[chatId] ?? state.pmaArtifacts.__global__ ?? [] : state.pmaArtifacts.__global__ ?? [];
}

export function selectReadMarkers(state: ReadModelEntityState): Record<string, string> {
  return state.readMarkers;
}

export function scopedOwnerKey(owner: Exclude<TicketOwnerScope, null>): string {
  return `${owner.kind}:${owner.id}`;
}

export function selectTicketSummaries(state: ReadModelEntityState, ownerKey: string): TicketSummary[] {
  return (state.ticketOrderByOwner[ownerKey] ?? []).map((id) => state.ticketSummaries[id]).filter(Boolean);
}

export function selectPmaRuns(state: ReadModelEntityState, ownerKey: string): PmaRunProgress[] {
  return (state.pmaRunOrderByOwner[ownerKey] ?? []).map((id) => state.pmaRuns[id]).filter(Boolean);
}

export function selectTicketListView(
  state: ReadModelEntityState,
  owner: Exclude<TicketOwnerScope, null>,
  actionManifest: SurfaceActionManifest | null = null
): TicketListViewModel {
  const ownerKey = scopedOwnerKey(owner);
  return buildTicketListViewModel(
    {
      tickets: selectTicketSummaries(state, ownerKey),
      runs: selectPmaRuns(state, ownerKey),
      chats: selectPmaChats(state),
      artifacts: [] as SurfaceArtifact[]
    },
    owner,
    actionManifest
  );
}

export function pmaChatCounters(chats: PmaChatSummary[]): ChatIndexCounters {
  return {
    total: chats.length,
    waiting: chats.filter((chat) => chat.status === 'waiting').length,
    running: chats.filter((chat) => chat.status === 'running').length,
    unread: 0,
    archived: chats.filter((chat) => chat.lifecycleStatus === 'archived').length
  };
}

export function selectRepoSummaries(state: ReadModelEntityState): RepoSummary[] {
  return state.repoOrder
    .map((repoId) => state.repos[repoId])
    .filter(Boolean)
    .map((repo) =>
      mapRepoSummary({
        id: repo.repoId,
        name: repo.label,
        path: repo.path,
        kind: 'base',
        worktree_count: repo.childWorktreeIds.length,
        ...runtimeRaw(state.runtime[`repo:${repo.repoId}`])
      })
    );
}

export function selectWorktreeSummaries(state: ReadModelEntityState): WorktreeSummary[] {
  return state.worktreeOrder
    .map((worktreeId) => state.worktrees[worktreeId])
    .filter(Boolean)
    .map((worktree) =>
      mapWorktreeSummary({
        id: worktree.worktreeId,
        name: worktree.label,
        path: worktree.path,
        kind: 'worktree',
        worktree_of: worktree.repoId,
        branch: worktree.branch,
        ...runtimeRaw(state.runtime[`worktree:${worktree.worktreeId}`])
      })
    );
}

function surfaceFromRaw(raw: JsonRecord): ChatIndexRow['surface'] {
  const value = String(raw.surface_kind ?? raw.surface ?? raw.channel_kind ?? '').toLowerCase();
  if (value === 'discord') return 'discord';
  if (value === 'telegram') return 'telegram';
  if (value === 'app_server') return 'app_server';
  if (value === 'file_chat') return 'file_chat';
  if (value === 'pma' || value === 'managed_thread' || value === '') return 'pma';
  return 'other';
}

function surfaceFromKinds(kinds: unknown, surface: unknown): ChatIndexRow['surface'] {
  const surfaceRecord = surface && typeof surface === 'object' && !Array.isArray(surface) ? (surface as JsonRecord) : {};
  const first = Array.isArray(kinds) ? kinds.find((item) => typeof item === 'string') : null;
  return surfaceFromRaw({ surface_kind: first ?? surfaceRecord.surface_kind });
}

function legacyChatIndexStatus(
  lifecycle: string,
  lifecycleStatus: string,
  runtimeStatus: string,
  queueDepth: number
): ChatIndexRow['status'] {
  if (lifecycleStatus === 'archived') return 'archived';
  if (queueDepth > 0) return 'waiting';
  if (lifecycle === 'running' || runtimeStatus === 'running') return 'running';
  if (['failed', 'error', 'blocked', 'invalid'].includes(runtimeStatus)) return 'failed';
  return 'idle';
}

function chatIndexStatus(chat: PmaChatSummary): ChatIndexRow['status'] {
  if (chat.lifecycleStatus === 'archived') return 'archived';
  if (chat.status === 'waiting') return 'waiting';
  if (chat.status === 'running') return 'running';
  if (chat.status === 'failed' || chat.status === 'blocked' || chat.status === 'invalid') return 'failed';
  return 'idle';
}

function runtimeRaw(row: RuntimeProjection | undefined): JsonRecord {
  return row
    ? {
        active_runs: row.activeRunId ? 1 : 0,
        open_tickets: row.waitingTicketCount + row.runningTicketCount,
        ticket_flow_display: {
          status: normalizeRuntimeStatus(row.activeRunStatus),
          is_active: Boolean(row.activeRunId),
          total_count: row.waitingTicketCount + row.runningTicketCount,
          done_count: 0,
          run_id: row.activeRunId
        },
        git_status: {
          dirty: row.gitDirty,
          ahead: row.gitAhead,
          behind: row.gitBehind
        },
        chat_bound_thread_count: row.chatCount
      }
    : {};
}

function normalizeRuntimeStatus(status: string | null | undefined): WorkStatus | string | null {
  return status ? normalizeWorkStatus(status) : status ?? null;
}

function stringValue(value: unknown, fallback: string | null = null): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}
