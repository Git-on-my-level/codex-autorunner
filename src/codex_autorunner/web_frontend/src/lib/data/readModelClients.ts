import { mapResult, pmaApi, type ApiResult, type PmaApiClient } from '$lib/api/client';
import {
  mapReadModelContract,
  type ChatDetailSnapshot,
  type ChatIndexSnapshot,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';

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
        filter: request.filter ?? 'all',
        limit: String(request.limit ?? 50)
      });
      if (request.query) params.set('query', request.query);
      if (request.cursor) params.set('cursor', request.cursor);
      return mapResult(await api.getJson<JsonRecord>(`/hub/read-models/chats?${params.toString()}`), (payload) =>
        mapReadModelContract<ChatIndexSnapshot>(payload)
      );
    },
    chatDetail: async (chatId, timelineLimit = 50) => {
      const params = new URLSearchParams({ timeline_limit: String(timelineLimit) });
      return mapResult(await api.getJson<JsonRecord>(`/hub/read-models/chats/${encodeURIComponent(chatId)}?${params.toString()}`), (payload) =>
        mapReadModelContract<ChatDetailSnapshot>(payload)
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
