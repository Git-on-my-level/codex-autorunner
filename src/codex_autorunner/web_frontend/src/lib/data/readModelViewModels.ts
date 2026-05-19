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
  pmaLifecycleTokenIsActive,
  pmaLifecycleTokenIsArchived,
  pmaChatArchivedFromRawSignals,
  type PmaChatSummary,
  type PmaRunProgress,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketSummary,
  type WorktreeSummary,
  type WorkStatus
} from '$lib/viewModels/domain';
import { normalizeManagedThreadChatKind } from '$lib/viewModels/managedThreadChatKind';
import type { PmaQueuedTurn } from '$lib/api/client';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';
import { selectChatIndexWindowView, type ChatIndexWindowRequest, type ReadModelEntityState } from './readModelStore';

type JsonRecord = Record<string, unknown>;
type ChatTranscriptProjection = ReadModelEntityState['chatTranscripts'][string];

const emptyChatTranscript: ChatTranscriptCard[] = [];
const chatTranscriptSelectionCache = new WeakMap<ChatTranscriptProjection, ChatTranscriptCard[]>();

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
    unreadCount: chat.unreadCount ?? unreadCountFromRaw(chat.raw),
    lastActivityAt: chat.updatedAt,
    repoId: chat.repoId,
    worktreeId: chat.worktreeId,
    ticketId: chat.ticketId,
    runId: chat.runId ?? null,
    agent: chat.agentId,
    agentProfile: chat.agentProfile,
    chatKind:
      chat.chatKind ??
      normalizeManagedThreadChatKind(chat.raw.chat_kind ?? chat.raw.thread_kind),
    model: chat.model,
    groupId: chat.ticketId ? `ticket:${chat.ticketId}` : chat.runId ? `run:${chat.runId}` : null,
    flowType: chat.flowType === 'ticket_flow' ? 'ticket_flow' : null,
    ticketPath: stringValue(chat.raw.ticket_path ?? chat.raw.ticketPath),
    ticketDone: chat.ticketDone ?? booleanOrNull(chat.raw.ticket_done ?? chat.raw.ticketDone),
    ticketStatus: ticketStatusValue(chat.raw.ticket_status ?? chat.raw.ticketStatus)
  };
}

export function legacyChatIndexRecordToChatIndexRow(raw: JsonRecord): ChatIndexRow {
  const managedThreadId = stringValue(raw.managed_thread_id ?? raw.thread_target_id);
  const rowId = stringValue(raw.row_id, 'unknown-chat-row');
  const chatId = managedThreadId ?? rowId ?? 'unknown-chat-row';
  const resourceKind = stringValue(raw.resource_kind);
  const resourceId = stringValue(raw.resource_id);
  const worktreeId = stringValue(raw.worktree_id ?? raw.worktree_repo_id) ?? (resourceKind === 'worktree' ? resourceId : null);
  const queueDepth = numberValue(raw.queue_depth);
  const lifecycle = stringValue(raw.lifecycle)?.toLowerCase() ?? '';
  const lifecycleStatus = stringValue(raw.lifecycle_status)?.toLowerCase() ?? '';
  const runtimeStatus = stringValue(raw.runtime_status ?? raw.target_runtime_status)?.toLowerCase() ?? '';
  const rawTitle = stringValue(raw.title ?? raw.display_name, chatId) ?? chatId;
  const title = rawTitle.trim() || chatId;
  return {
    chatId,
    surface: surfaceFromKinds(raw.surface_kinds, raw.surface),
    title,
    status: legacyChatIndexStatus(lifecycle, lifecycleStatus, runtimeStatus, queueDepth),
    unreadCount: numberValue(raw.unread_count ?? raw.unreadCount) || (raw.unread === true ? 1 : 0),
    lastActivityAt: stringValue(raw.last_activity_at ?? raw.updated_at ?? raw.created_at),
    repoId: stringValue(raw.repo_id),
    worktreeId,
    ticketId: resourceKind === 'ticket' ? resourceId : stringValue(raw.ticket_id ?? raw.current_ticket_id),
    runId: resourceKind === 'run' || resourceKind === 'ticket_run' ? resourceId : stringValue(raw.run_id),
    agent: stringValue(raw.agent ?? raw.agent_id),
    agentProfile: stringValue(raw.agent_profile ?? raw.agentProfile),
    chatKind: normalizeManagedThreadChatKind(raw.chat_kind ?? raw.chatKind ?? raw.thread_kind),
    model: stringValue(raw.model),
    groupId: stringValue(raw.group_id),
    flowType: stringValue(raw.flow_type ?? raw.flowType) === 'ticket_flow' ? 'ticket_flow' : null,
    ticketPath: stringValue(raw.ticket_path ?? raw.ticketPath),
    ticketDone: booleanOrNull(raw.ticket_done ?? raw.ticketDone),
    ticketStatus: ticketStatusValue(raw.ticket_status ?? raw.ticketStatus)
  };
}

export function chatIndexRowToPmaChatSummary(row: ChatIndexRow): PmaChatSummary {
  const title = row.displayTitle ?? row.bindingDisplayName ?? row.title;
  const raw: JsonRecord = {
    row,
    id: row.chatId,
    managed_thread_id: row.chatId,
    title,
    display_name: title,
    technical_title: row.technicalTitle,
    binding_display_name: row.bindingDisplayName,
    binding_display_names: row.bindingDisplayNames ?? [],
    primary_surface: row.primarySurface,
    surface_bindings: row.surfaceBindings ?? [],
    normalized_status: row.runtimeStatus ?? row.status,
    runtime_status: row.runtimeStatus ?? row.status,
    status: row.status,
    lifecycle: row.lifecycle,
    lifecycle_status: row.archiveState ?? (row.status === 'archived' ? 'archived' : 'active'),
    archive_state: row.archiveState,
    resource_kind: row.resourceKind,
    resource_id: row.resourceId,
    workspace_root: row.workspaceRoot,
    repo_id: row.repoId,
    worktree_id: row.worktreeId,
    current_ticket_id: row.ticketId,
    ticket_id: row.ticketId,
    ticket_path: row.ticketPath,
    ticket_done: row.ticketDone,
    ticket_status: row.ticketStatus,
    run_id: row.runId,
    flow_type: row.flowType,
    unread_count: row.unreadCount,
    agent_id: row.agent,
    agent_profile: row.agentProfile,
    chat_kind: row.chatKind,
    model: row.model,
    unreadCount: row.unreadCount,
    last_activity_at: row.lastActivityAt,
    surface_kind: row.surface,
    sort_key: row.sortKey
  };
  return {
    id: row.chatId,
    title,
    lifecycleStatus: row.archiveState ?? (row.status === 'archived' ? 'archived' : 'active'),
    status: normalizeWorkStatus(row.status),
    agentId: row.agent ?? null,
    chatKind: row.chatKind ?? null,
    agentProfile: row.agentProfile ?? null,
    model: row.model ?? null,
    repoId: row.repoId ?? null,
    worktreeId: row.worktreeId ?? null,
    ticketId: row.ticketId ?? null,
    ticketPath: row.ticketPath ?? null,
    runId: row.runId ?? null,
    unreadCount: row.unreadCount,
    flowType: row.flowType ?? null,
    isTicketFlow: Boolean(
      row.flowType === 'ticket_flow' ||
      row.ticketId ||
        row.runId ||
        row.groupId?.startsWith('ticket') ||
        row.groupId?.startsWith('run')
    ),
    ticketDone: row.ticketDone ?? null,
    progressPercent: null,
    updatedAt: row.lastActivityAt ?? null,
    raw
  };
}

export function selectPmaChats(state: ReadModelEntityState, request?: ChatIndexWindowRequest): PmaChatSummary[] {
  if (request) {
    return selectChatIndexWindowView(state, request).rows.map(chatIndexRowToPmaChatSummary);
  }
  return state.chatOrder.map((id) => state.chats[id]).filter(Boolean).map(chatIndexRowToPmaChatSummary);
}

export function selectChatTranscript(state: ReadModelEntityState, chatId: string | null): ChatTranscriptCard[] {
  if (!chatId) return emptyChatTranscript;
  const transcript = state.chatTranscripts[chatId];
  if (!transcript) return emptyChatTranscript;
  const cached = chatTranscriptSelectionCache.get(transcript);
  if (cached) return cached;
  const selected = transcript.order.map((id) => transcript.cardsById[id]).filter(Boolean);
  chatTranscriptSelectionCache.set(transcript, selected);
  return selected;
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
    unread: chats.reduce((total, chat) => total + unreadCountFromRaw(chat.raw), 0),
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
        is_pinned: Boolean(repo.isPinned),
        ...(Array.isArray(repo.worktreeSetupCommands)
          ? { worktree_setup_commands: repo.worktreeSetupCommands }
          : {}),
        ...runtimeRaw(state.runtime[`repo:${repo.repoId}`]),
        chat_bound: Boolean(repo.chatBound),
        chat_bound_thread_count: repo.chatBindingCount ?? 0,
        chat_binding_sources: repo.chatBindingSources ?? {},
        chat_binding_display_names: repo.chatBindingDisplayNames ?? []
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
        ...runtimeRaw(state.runtime[`worktree:${worktree.worktreeId}`]),
        chat_bound: Boolean(worktree.chatBound),
        chat_bound_thread_count: worktree.chatBindingCount ?? 0,
        chat_binding_sources: worktree.chatBindingSources ?? {},
        chat_binding_display_names: worktree.chatBindingDisplayNames ?? []
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
  if (pmaLifecycleTokenIsArchived(chat.lifecycleStatus)) return 'archived';
  if (!pmaLifecycleTokenIsActive(chat.lifecycleStatus) && pmaChatArchivedFromRawSignals(chat.raw)) return 'archived';
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
        chat_bound: row.chatCount > 0,
        chat_bound_thread_count: row.chatCount,
        cleanup_blocked_by_chat_binding: row.cleanupBlockers.includes('chat_binding')
      }
    : {};
}

function normalizeRuntimeStatus(status: string | null | undefined): WorkStatus | string | null {
  return status ? normalizeWorkStatus(status) : status ?? null;
}

function stringValue(value: unknown, fallback: string | null = null): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function booleanOrNull(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase();
  if (['true', '1', 'yes'].includes(normalized)) return true;
  if (['false', '0', 'no'].includes(normalized)) return false;
  return null;
}

function ticketStatusValue(value: unknown): ChatIndexRow['ticketStatus'] {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase();
  if (['done', 'running', 'waiting', 'failed', 'unknown'].includes(normalized)) {
    return normalized as ChatIndexRow['ticketStatus'];
  }
  return null;
}

function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function unreadCountFromRaw(raw: JsonRecord): number {
  const row = raw.row && typeof raw.row === 'object' && !Array.isArray(raw.row) ? (raw.row as JsonRecord) : {};
  const count = numberValue(raw.unread_count ?? raw.unreadCount ?? row.unread_count ?? row.unreadCount);
  if (count > 0) return count;
  return raw.unread === true || row.unread === true ? 1 : 0;
}
