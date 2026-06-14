import { mapResult, webApi, type ApiResult, type AutomationWorkspace, type WebApiClient } from '$lib/api/client';
import type { TicketSummary } from '$lib/viewModels/domain';
import {
  normalizeChatFacetRequest,
  mapReadModelContract,
  type ChatFacetRequest,
  type ChatDetailSnapshot,
  type ChatIndexSnapshot,
  type PreviewServicesReadModel,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';

type JsonRecord = Record<string, unknown>;

export type ChatIndexRequest = {
  filter?: ChatIndexSnapshot['filter'];
  query?: string | null;
  facets?: Partial<ChatFacetRequest> | null;
  surfaceKind?: string | null;
  groupBy?: 'ticket_run' | null;
  parentGroupId?: string | null;
  cursor?: string | null;
  limit?: number;
};

export type ReadModelSnapshotClient = {
  chatIndex(request?: ChatIndexRequest): Promise<ApiResult<ChatIndexSnapshot>>;
  chatDetail(chatId: string, timelineLimit?: number): Promise<ApiResult<ChatDetailSnapshot>>;
  automationWorkspaceIndex(): Promise<ApiResult<AutomationWorkspace>>;
  repoWorktreeTopology(kind?: 'all' | 'repo' | 'worktree', limit?: number, cursor?: string | null): Promise<ApiResult<RepoWorktreeTopologySnapshot>>;
  repoWorktreeRuntime(kind?: 'all' | 'repo' | 'worktree', limit?: number, cursor?: string | null): Promise<ApiResult<RepoWorktreeRuntimeSnapshot>>;
  repoDetail(
    repoId: string,
    options?: { ticketLimit?: number; ticketCursor?: string | null }
  ): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
  worktreeDetail(
    worktreeId: string,
    options?: { ticketLimit?: number; ticketCursor?: string | null }
  ): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
  servicesReadModel(scope?: string | null): Promise<ApiResult<PreviewServicesReadModel>>;
  ticketDetail(ticketId: string, owner: { kind: 'repo' | 'worktree'; id: string }): Promise<ApiResult<TicketDetailSnapshot>>;
  ticketIndex(owner?: { repo?: string; worktree?: string }): Promise<ApiResult<TicketSummary[]>>;
};

export function createReadModelSnapshotClient(api: WebApiClient = webApi): ReadModelSnapshotClient {
  return {
    chatIndex: async (request = {}) => {
      const params = new URLSearchParams({
        filter: request.filter ?? 'all',
        limit: String(request.limit ?? 50)
      });
      if (request.query) params.set('search', request.query);
      if (request.surfaceKind) params.set('surface_kind', request.surfaceKind);
      if (request.groupBy) params.set('group_by', request.groupBy);
      if (request.parentGroupId) params.set('parent_group_id', request.parentGroupId);
      appendChatFacetParams(params, normalizeChatFacetRequest(request.facets));
      if (request.cursor) params.set('offset', request.cursor);
      return mapResult(await api.getJson<JsonRecord>(`/hub/read-models/chats?${params.toString()}`), (payload) =>
        mapReadModelContract<ChatIndexSnapshot>(payload)
      );
    },
    chatDetail: async (chatId, timelineLimit = 50) => {
      const params = new URLSearchParams({ timeline_limit: String(timelineLimit) });
      return mapResult(
        await api.getJson<JsonRecord>(
          `/hub/read-models/chats/${encodeURIComponent(chatId)}?${params.toString()}`
        ),
        (payload) => mapReadModelContract<ChatDetailSnapshot>(payload)
      );
    },
    automationWorkspaceIndex: () => api.hub.getAutomationWorkspaceIndex(),
    repoWorktreeTopology: (kind, limit, cursor) => api.readModels.repoWorktreeTopology(kind, limit, cursor),
    repoWorktreeRuntime: (kind, limit, cursor) => api.readModels.repoWorktreeRuntime(kind, limit, cursor),
    repoDetail: (repoId, options) => api.readModels.repoDetail(repoId, options),
    worktreeDetail: (worktreeId, options) => api.readModels.worktreeDetail(worktreeId, options),
    servicesReadModel: (scope) => api.hub.getServicesReadModel(scope),
    ticketDetail: async (ticketId, owner) =>
      mapResult(await api.readModels.ticketDetail(ticketId, owner), (payload) => mapReadModelContract<TicketDetailSnapshot>(payload)),
    ticketIndex: async (owner) => api.ticketFlow.listTickets(owner)
  };
}

export const readModelSnapshotClient = createReadModelSnapshotClient();

function appendChatFacetParams(params: URLSearchParams, facets: ChatFacetRequest): void {
  appendRepeated(params, 'category', facets.categories);
  appendRepeated(params, 'turn_kind', facets.turnKinds);
  appendRepeated(params, 'origin_kind', facets.originKinds);
  appendRepeated(params, 'transport', facets.transports);
  appendRepeated(params, 'scope_kind', facets.scopeKinds);
  appendRepeated(params, 'scope_id', facets.scopeIds);
  appendRepeated(params, 'agent_kind', facets.agentKinds);
}

function appendRepeated(params: URLSearchParams, key: string, values?: string[] | null): void {
  for (const value of values ?? []) {
    if (value) params.append(key, value);
  }
}
