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
  pmaLifecycleTokenIsArchived,
  pmaChatArchivedFromRawSignals,
  type PmaChatSummary,
  type PmaRunProgress,
  type PmaTimelineItem,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketSummary,
  type WorktreeSummary,
  type WorkStatus
} from '$lib/viewModels/domain';
import { normalizeManagedThreadChatKind } from '$lib/viewModels/managedThreadChatKind';
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
    groupId: chat.ticketId ? `ticket:${chat.ticketId}` : chat.runId ? `run:${chat.runId}` : null
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
    unread_count: row.unreadCount,
    agent_id: row.agent,
    agent_profile: row.agentProfile,
    chat_kind: row.chatKind,
    model: row.model,
    unreadCount: row.unreadCount,
    last_activity_at: row.lastActivityAt,
    surface_kind: row.surface
  };
  return {
    id: row.chatId,
    title: row.title,
    lifecycleStatus: row.status === 'archived' ? 'archived' : 'active',
    status: normalizeWorkStatus(row.status),
    agentId: row.agent ?? null,
    chatKind: row.chatKind ?? null,
    agentProfile: row.agentProfile ?? null,
    model: row.model ?? null,
    repoId: row.repoId ?? null,
    worktreeId: row.worktreeId ?? null,
    ticketId: row.ticketId ?? null,
    runId: row.runId ?? null,
    unreadCount: row.unreadCount,
    flowType: null,
    isTicketFlow: Boolean(
      row.ticketId ||
        row.runId ||
        row.groupId?.startsWith('ticket') ||
        row.groupId?.startsWith('run') ||
        /^ticket-flow(?::\S+)?$/i.test(row.title.trim())
    ),
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

function chatIndexStatus(chat: PmaChatSummary): ChatIndexRow['status'] {
  if (pmaLifecycleTokenIsArchived(chat.lifecycleStatus) || pmaChatArchivedFromRawSignals(chat.raw))
    return 'archived';
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
