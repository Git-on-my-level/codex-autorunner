import {
  mapReadModelContract,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import {
  mapContextspaceDocument,
  mapDashboardSummary,
  mapPmaChatMessage,
  mapPmaTimelineItem,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapTicketSummary,
  mapWorktreeSummary,
  type ContextspaceDocument,
  type DashboardSummary,
  type PmaChatMessage,
  type PmaChatSummary,
  type PmaRunProgress,
  type PmaTimelineItem,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketDetail,
  type TicketSummary,
  type WorktreeSummary
} from '$lib/viewModels/domain';
import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';

export type ApiErrorKind = 'http' | 'network' | 'parse' | 'aborted';

export type ApiError = {
  kind: ApiErrorKind;
  status: number | null;
  code: string;
  message: string;
  details?: unknown;
};

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiError };

export type Loadable<T> =
  | { state: 'idle'; data?: undefined; error?: undefined }
  | { state: 'loading'; data?: T; error?: undefined }
  | { state: 'ready'; data: T; error?: undefined }
  | { state: 'error'; data?: T; error: ApiError };

export type PartialPageIssue = {
  id: string;
  title: string;
  message: string;
  retryLabel: string;
};

export type JsonRecord = Record<string, unknown>;

export type PmaQueuedTurn = {
  managedTurnId: string;
  position: number;
  state: string;
  prompt: string;
  promptPreview: string;
  attachments: JsonRecord[];
  model: string | null;
  reasoning: string | null;
  enqueuedAt: string | null;
  raw: JsonRecord;
};

export type PmaThreadQueue = {
  managedThreadId: string;
  queueDepth: number;
  queuedTurns: PmaQueuedTurn[];
};

export type PmaBulkArchiveResult = {
  threads: PmaChatSummary[];
  archivedCount: number;
  requestedCount: number;
  errorCount: number;
  errors: JsonRecord[];
};

export type WorktreeCleanupRequest = {
  worktreeRepoId: string;
  archive?: boolean;
  force?: boolean;
  forceAttestation?: string | null;
  forceArchive?: boolean;
  archiveNote?: string | null;
};

export type RepoStateArchiveRequest = {
  kind: 'repo' | 'worktree';
  id: string;
  archiveNote?: string | null;
};

export type CreateRepoRequest = {
  repoId?: string | null;
  gitUrl?: string | null;
  path?: string | null;
  gitInit?: boolean;
  force?: boolean;
};

export type CreateWorktreeRequest = {
  baseRepoId: string;
  branch: string;
  startPoint?: string | null;
  force?: boolean;
};

export type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: unknown;
};

const defaultHeaders = {
  accept: 'application/json'
};

export function normalizeApiError(error: unknown, status: number | null = null): ApiError {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return {
      kind: 'aborted',
      status,
      code: 'request_aborted',
      message: 'Request was cancelled.'
    };
  }
  if (error instanceof Error) {
    return {
      kind: status === null ? 'network' : 'http',
      status,
      code: status === null ? 'network_error' : `http_${status}`,
      message: error.message || 'Request failed.'
    };
  }
  if (typeof error === 'string' && error.trim()) {
    return {
      kind: status === null ? 'network' : 'http',
      status,
      code: status === null ? 'network_error' : `http_${status}`,
      message: error
    };
  }
  return {
    kind: status === null ? 'network' : 'http',
    status,
    code: status === null ? 'network_error' : `http_${status}`,
    message: 'Request failed.'
  };
}

export async function parseApiErrorResponse(response: Response): Promise<ApiError> {
  let details: unknown;
  let message = response.statusText || `Request failed with ${response.status}`;
  let code = `http_${response.status}`;

  try {
    details = await response.clone().json();
  } catch {
    try {
      const text = await response.text();
      if (text.trim()) message = readableErrorText(text.trim(), response.status);
    } catch {
      // Ignore secondary parse failures and keep the HTTP status message.
    }
  }

  if (details && typeof details === 'object') {
    const record = details as JsonRecord;
    const detail = record.detail;
    const error = record.error;
    const maybeCode = record.code;
    if (typeof maybeCode === 'string' && maybeCode.trim()) code = maybeCode;
    if (typeof detail === 'string' && detail.trim()) message = detail;
    else if (typeof error === 'string' && error.trim()) message = error;
  }

  return {
    kind: 'http',
    status: response.status,
    code,
    message,
    details
  };
}

function readableErrorText(text: string, status: number): string {
  if (/^\s*<!doctype html/i.test(text) || /^\s*<html[\s>]/i.test(text)) {
    return `Server returned an HTML error page for request ${status}.`;
  }
  return text.length > 220 ? `${text.slice(0, 217)}...` : text;
}

export class PmaApiClient {
  constructor(
    private readonly fetcher: typeof fetch = fetch,
    private readonly basePath = runtimeBasePath()
  ) {}

  async requestJson<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
    const headers = new Headers(defaultHeaders);
    if (options.body !== undefined) headers.set('content-type', 'application/json');
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));

    try {
      const response = await this.fetcher(this.url(path), {
        ...options,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        headers
      });
      if (!response.ok) {
        return { ok: false, error: await parseApiErrorResponse(response) };
      }
      if (response.status === 204) {
        return { ok: true, data: undefined as T };
      }
      try {
        return { ok: true, data: (await response.json()) as T };
      } catch (error) {
        return {
          ok: false,
          error: {
            ...normalizeApiError(error, response.status),
            kind: 'parse',
            code: 'invalid_json',
            message: 'Server returned invalid JSON.'
          }
        };
      }
    } catch (error) {
      return { ok: false, error: normalizeApiError(error) };
    }
  }

  async getJson<T>(path: string): Promise<ApiResult<T>> {
    return this.requestJson<T>(path);
  }

  async uploadForm<T>(path: string, body: FormData): Promise<ApiResult<T>> {
    try {
      const response = await this.fetcher(this.url(path), {
        method: 'POST',
        body,
        headers: { accept: 'application/json' }
      });
      if (!response.ok) {
        return { ok: false, error: await parseApiErrorResponse(response) };
      }
      return { ok: true, data: (await response.json()) as T };
    } catch (error) {
      return { ok: false, error: normalizeApiError(error) };
    }
  }

  url(path: string): string {
    return withRuntimeBasePath(path, this.basePath);
  }

  pma = {
    // Legacy diagnostics/tests only. Screen routes use chat index/detail projections.
    listChats: async (status: 'active' | 'archived' | null = 'active'): Promise<ApiResult<PmaChatSummary[]>> => {
      const query = status ? `?status=${encodeURIComponent(status)}` : '';
      return mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads${query}`), (payload) =>
        asArray(payload.threads).map(mapPmaChatSummary)
      );
    },
    createChat: async (body: unknown): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/threads', {
          method: 'POST',
          body
        }),
        (payload) => mapPmaChatSummary(asRecord(payload.thread ?? payload))
      ),
    getChat: async (chatId: string): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}`), (payload) =>
        mapPmaChatSummary(asRecord(payload.thread))
      ),
    sendMessage: async (chatId: string, body: unknown): Promise<ApiResult<PmaChatMessage>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/messages`, {
          method: 'POST',
          body
        }),
        (payload) => mapPmaChatMessage(asRecord(payload.message ?? payload.turn ?? payload))
      ),
    forkThread: async (chatId: string, body: unknown): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/fork`, {
          method: 'POST',
          body
        }),
        (payload) => mapPmaChatSummary(asRecord(payload.thread ?? payload))
      ),
    getTimeline: async (chatId: string): Promise<ApiResult<PmaTimelineItem[]>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/timeline`), (payload) =>
        asArray(payload.items).map(mapPmaTimelineItem)
      ),
    getTail: async (chatId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/tail`), mapPmaRunProgress),
    getStatus: async (chatId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/status`), mapPmaRunProgress),
    getQueue: async (chatId: string): Promise<ApiResult<PmaThreadQueue>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/queue`), mapPmaThreadQueue),
    interruptThread: async (chatId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/interrupt`, { method: 'POST' }),
    resumeThread: async (chatId: string): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/resume`, {
          method: 'POST',
          body: {}
        }),
        (payload) => mapPmaChatSummary(asRecord(payload.thread))
      ),
    compactThread: async (chatId: string, summary: string): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/compact`, {
          method: 'POST',
          body: { summary, reset_backend: true }
        }),
        (payload) => mapPmaChatSummary(asRecord(payload.thread))
      ),
    archiveThread: async (chatId: string): Promise<ApiResult<PmaChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/archive`, {
          method: 'POST'
        }),
        (payload) => mapPmaChatSummary(asRecord(payload.thread))
      ),
    archiveThreads: async (chatIds: string[]): Promise<ApiResult<PmaBulkArchiveResult>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/threads/archive', {
          method: 'POST',
          body: { thread_ids: chatIds }
        }),
        (payload) => ({
          threads: asArray(payload.threads).map(mapPmaChatSummary),
          archivedCount: numberValue(payload.archived_count ?? payload.archivedCount, 0),
          requestedCount: numberValue(payload.requested_count ?? payload.requestedCount, chatIds.length),
          errorCount: numberValue(payload.error_count ?? payload.errorCount, 0),
          errors: asArray(payload.errors)
        })
      ),
    cancelQueuedTurn: async (chatId: string, turnId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(
        `/hub/pma/threads/${encodeURIComponent(chatId)}/queue/${encodeURIComponent(turnId)}/cancel`,
        { method: 'POST' }
      ),
    clearQueue: async (chatId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/queue/clear`, { method: 'POST' }),
    listFiles: async (): Promise<ApiResult<SurfaceArtifact[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/files'), (payload) =>
        [...asArray(payload.inbox), ...asArray(payload.outbox)].map(mapSurfaceArtifact)
      ),
    uploadInboxFile: async (file: File): Promise<ApiResult<string[]>> => {
      const form = new FormData();
      form.append('file', file, file.name);
      return mapResult(await this.uploadForm<JsonRecord>('/hub/pma/files/inbox', form), (payload) =>
        Array.isArray(payload.saved) ? payload.saved.filter((name): name is string => typeof name === 'string') : []
      );
    },
    listAgents: async (): Promise<ApiResult<{ agents: JsonRecord[]; default: string; defaults: JsonRecord }>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/agents'), (payload) => ({
        agents: asArray(payload.agents),
        default: typeof payload.default === 'string' ? payload.default : '',
        defaults: asRecord(payload.defaults)
      })),
    listAgentModels: async (agentId: string): Promise<ApiResult<JsonRecord[]>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/agents/${encodeURIComponent(agentId)}/models`), (payload) =>
        asArray(payload.models)
      ),
    listDocs: async (): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/docs'), (payload) =>
        asArray(payload.docs ?? payload.documents).map(mapContextspaceDocument)
      ),
    getDoc: async (name: string): Promise<ApiResult<ContextspaceDocument>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/docs/${encodeURIComponent(name)}`), (payload) =>
        mapContextspaceDocument({
          ...payload,
          id: payload.name ?? name,
          kind: payload.name ?? name,
          is_pinned: true
        })
      ),
    updateDoc: async (name: string, content: string): Promise<ApiResult<ContextspaceDocument>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/docs/${encodeURIComponent(name)}`, {
          method: 'PUT',
          body: { content }
        }),
        (payload) =>
          mapContextspaceDocument({
            ...payload,
            id: payload.name ?? name,
            name: payload.name ?? name,
            kind: payload.name ?? name,
            content,
            is_pinned: true
          })
      ),
    listDocsWithContent: async (): Promise<ApiResult<ContextspaceDocument[]>> => {
      const docs = await this.pma.listDocs();
      if (!docs.ok) return docs;
      const visibleDocs = docs.data.filter((doc) => isStandardPmaDoc(doc.name));
      const hydrated = await Promise.all(visibleDocs.map((doc) => this.pma.getDoc(doc.name)));
      const firstError = hydrated.find((result) => !result.ok);
      if (firstError && !firstError.ok) return firstError;
      return {
        ok: true,
        data: hydrated.map((result, index) => ({
          ...(result.ok ? result.data : visibleDocs[index]),
          ...visibleDocs[index],
          content: result.ok ? result.data.content : visibleDocs[index].content
        }))
      };
    }
  };

  hub = {
    getDashboard: async (): Promise<ApiResult<DashboardSummary>> =>
      mapResult(
        await this.getJson<JsonRecord>('/hub/messages?sections=inbox,managed_threads,pma_files_detail,automation,action_queue,freshness'),
        mapDashboardSummary
      ),
    // Legacy diagnostics and mutation follow-up only. Screen inventory uses repo/worktree read models.
    listRepos: async (): Promise<ApiResult<RepoSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/repos'), (payload) =>
        asArray(payload.repos ?? payload.items).filter((item) => !isWorktreeItem(item)).map(mapRepoSummary)
      ),
    // Legacy diagnostics and mutation follow-up only. Screen inventory uses repo/worktree read models.
    listWorktrees: async (): Promise<ApiResult<WorktreeSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/repos'), (payload) =>
        asArray(payload.worktrees ?? payload.repos ?? payload.items).filter(isWorktreeItem).map(mapWorktreeSummary)
      ),
    createRepo: async (request: CreateRepoRequest): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/hub/repos', {
        method: 'POST',
        body: {
          repoId: request.repoId ?? null,
          gitUrl: request.gitUrl ?? null,
          path: request.path ?? null,
          gitInit: request.gitInit ?? true,
          force: request.force ?? false
        }
      }),
    createWorktree: async (request: CreateWorktreeRequest): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/hub/worktrees/create', {
        method: 'POST',
        body: {
          baseRepoId: request.baseRepoId,
          branch: request.branch,
          startPoint: request.startPoint ?? null,
          force: request.force ?? false
        }
      }),
    cleanupWorktree: async (request: WorktreeCleanupRequest): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/hub/worktrees/cleanup', {
        method: 'POST',
        body: {
          worktreeRepoId: request.worktreeRepoId,
          archive: request.archive ?? true,
          force: request.force ?? false,
          forceAttestation: request.forceAttestation ?? null,
          forceArchive: request.forceArchive ?? false,
          archiveNote: request.archiveNote ?? null
        }
      }),
    archiveState: async (request: RepoStateArchiveRequest): Promise<ApiResult<JsonRecord>> => {
      const path = request.kind === 'repo' ? '/hub/repos/archive-state' : '/hub/worktrees/archive-state';
      const idKey = request.kind === 'repo' ? 'repoId' : 'worktreeRepoId';
      return this.requestJson<JsonRecord>(path, {
        method: 'POST',
        body: {
          [idKey]: request.id,
          archiveNote: request.archiveNote ?? null
        }
      });
    },
    syncRepoMain: async (repoId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/repos/${encodeURIComponent(repoId)}/sync-main`, {
        method: 'POST'
      }),
    setWorktreeSetup: async (repoId: string, commands: string[]): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/repos/${encodeURIComponent(repoId)}/worktree-setup`, {
        method: 'POST',
        body: { commands }
      })
  };

  readModels = {
    repoWorktreeTopology: async (
      kind: 'all' | 'repo' | 'worktree' = 'all',
      limit = 50,
      cursor?: string | null
    ): Promise<ApiResult<RepoWorktreeTopologySnapshot>> => {
      const params = new URLSearchParams({ kind, limit: String(limit) });
      if (cursor) params.set('cursor', cursor);
      return mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/repo-worktree/topology?${params.toString()}`),
        (payload) => mapReadModelContract<RepoWorktreeTopologySnapshot>(payload)
      );
    },
    repoWorktreeRuntime: async (
      kind: 'all' | 'repo' | 'worktree' = 'all',
      limit = 50,
      cursor?: string | null
    ): Promise<ApiResult<RepoWorktreeRuntimeSnapshot>> => {
      const params = new URLSearchParams({ kind, limit: String(limit) });
      if (cursor) params.set('cursor', cursor);
      return mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/repo-worktree/runtime?${params.toString()}`),
        (payload) => mapReadModelContract<RepoWorktreeRuntimeSnapshot>(payload)
      );
    },
    repoDetail: async (repoId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>> =>
      mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/repos/${encodeURIComponent(repoId)}/detail`),
        (payload) => mapReadModelContract<RepoWorktreeDetailSnapshot>(payload)
      ),
    worktreeDetail: async (worktreeId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>> =>
      mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/worktrees/${encodeURIComponent(worktreeId)}/detail`),
        (payload) => mapReadModelContract<RepoWorktreeDetailSnapshot>(payload)
      ),
    ticketDetail: async (
      ticketId: string,
      owner: { kind: 'repo' | 'worktree'; id: string }
    ): Promise<ApiResult<TicketDetailSnapshot & { legacyTicket?: JsonRecord; scopedTickets?: JsonRecord[]; scopedRuns?: JsonRecord[]; scopedChats?: JsonRecord[] }>> => {
      const params = new URLSearchParams({ owner_kind: owner.kind, owner_id: owner.id });
      return mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/tickets/${encodeURIComponent(ticketId)}?${params.toString()}`),
        (payload) =>
          mapReadModelContract<TicketDetailSnapshot & { legacyTicket?: JsonRecord; scopedTickets?: JsonRecord[]; scopedRuns?: JsonRecord[]; scopedChats?: JsonRecord[] }>(payload)
      );
    }
  };

  ticketFlow = {
    // Legacy diagnostics/tests only. Ticket and owner screens read scoped ticket/run projections.
    listRuns: async (owner?: { repo?: string; worktree?: string }): Promise<ApiResult<PmaRunProgress[]>> =>
      mapResult(await this.getJson<JsonRecord[]>(flowRunsPath(owner)), (payload) =>
        payload.map(mapPmaRunProgress)
      ),
    getRun: async (runId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/status`), mapPmaRunProgress),
    // Legacy diagnostics/tests only. Ticket and owner screens read scoped ticket projections.
    listTickets: async (owner?: { repo?: string; worktree?: string }): Promise<ApiResult<TicketSummary[]>> => {
      const hubResult = await this.getJson<JsonRecord>(hubTicketPath(owner));
      if (hubResult.ok || hubResult.error.status !== 404) {
        return mapResult(hubResult, (payload) => asArray(payload.tickets).map(mapTicketSummary));
      }
      if (!owner) return this.listLegacyMountedTickets();
      const legacyResult = await this.getJson<JsonRecord>(legacyTicketPath(owner));
      return mapResult(legacyResult, (payload) =>
        asArray(payload.tickets).map((ticket) => mapTicketSummary(ticketWithFallbackOwner(ticket, owner)))
      );
    },
    getTicket: async (index: number): Promise<ApiResult<TicketDetail>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/flows/ticket_flow/tickets/${encodeURIComponent(index)}`), mapTicketDetail),
    updateTicket: async (
      index: number,
      content: string,
      owner?: { repo?: string; worktree?: string }
    ): Promise<ApiResult<TicketDetail>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`${legacyTicketPath(owner)}/${encodeURIComponent(index)}`, {
          method: 'PUT',
          body: { content }
        }),
        mapTicketDetail
      ),
    createTicket: async (
      body: { agent?: string; title?: string; goal?: string; body?: string; profile?: string },
      owner?: { repo?: string; worktree?: string }
    ): Promise<ApiResult<TicketDetail>> =>
      mapResult(
        await this.requestJson<JsonRecord>(legacyTicketPath(owner), {
          method: 'POST',
          body
        }),
        mapTicketDetail
      ),
    reorderTicket: async (
      sourceIndex: number,
      destinationIndex: number,
      placeAfter: boolean,
      owner?: { repo?: string; worktree?: string }
    ): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`${legacyTicketPath(owner)}/reorder`, {
        method: 'POST',
        body: {
          source_index: sourceIndex,
          destination_index: destinationIndex,
          place_after: placeAfter
        }
      }),
    listArtifacts: async (runId: string, owner?: { repo?: string; worktree?: string }): Promise<ApiResult<SurfaceArtifact[]>> =>
      mapResult(await this.getJson<JsonRecord>(flowRunPath(runId, 'dispatch_history', owner)), (payload) =>
        asArray(payload.history).flatMap((entry) => asArray(entry.attachments)).map(mapSurfaceArtifact)
      ),
    getDispatchHistory: async (runId: string, owner?: { repo?: string; worktree?: string }): Promise<ApiResult<JsonRecord[]>> =>
      mapResult(await this.getJson<JsonRecord>(flowRunPath(runId, 'dispatch_history', owner)), (payload) =>
        asArray(payload.history)
      ),
    resumeRun: async (runId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/resume`, {
          method: 'POST'
        }),
        mapPmaRunProgress
      ),
    bootstrap: async (): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/api/flows/ticket_flow/bootstrap', {
          method: 'POST',
          body: { once: false }
        }),
        mapPmaRunProgress
      )
  };

  private async listLegacyMountedTickets(): Promise<ApiResult<TicketSummary[]>> {
    const topologyResult = await this.readModels.repoWorktreeTopology('all', 200);
    if (!topologyResult.ok) {
      const legacyResult = await this.getJson<JsonRecord>(legacyTicketPath());
      return mapResult(legacyResult, (payload) => asArray(payload.tickets).map((ticket) => mapTicketSummary(ticketWithFallbackOwner(ticket))));
    }
    const owners = [
      ...topologyResult.data.repos.map((repo) => ({ repo: repo.repoId })),
      ...topologyResult.data.worktrees.map((worktree) => ({ worktree: worktree.worktreeId }))
    ];
    const results = await Promise.all(
      owners.map(async (owner) => ({
        owner,
        result: await this.getJson<JsonRecord>(legacyTicketPath(owner))
      }))
    );
    const failed = results.find(({ result }) => !result.ok && result.error.status !== 404);
    if (failed && !failed.result.ok) return failed.result;
    return {
      ok: true,
      data: results.flatMap(({ owner, result }) =>
        result.ok ? asArray(result.data.tickets).map((ticket) => mapTicketSummary(ticketWithFallbackOwner(ticket, owner))) : []
      )
    };
  }

  contextspace = {
    listDocuments: async (workspaceId?: string): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(await this.getJson<JsonRecord>(contextspaceApiPath(workspaceId)), (payload) =>
        ['active_context', 'spec', 'decisions']
          .filter((kind) => typeof payload[kind] === 'string')
          .map((kind) =>
            mapContextspaceDocument({
              kind,
              name: contextspaceFilename(kind),
              content: payload[kind],
              is_pinned: true
            })
          )
      ),
    updateDocument: async (
      workspaceIdOrKind: string | undefined,
      kindOrContent: string,
      maybeContent?: string
    ): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(
        await this.requestJson<JsonRecord>(
          contextspaceUpdateApiPath(
            maybeContent === undefined ? undefined : workspaceIdOrKind,
            maybeContent === undefined ? workspaceIdOrKind || '' : kindOrContent
          ),
          {
            method: 'PUT',
            body: { content: maybeContent === undefined ? kindOrContent : maybeContent }
          }
        ),
        (payload) =>
          ['active_context', 'decisions', 'spec']
            .filter((docKind) => typeof payload[docKind] === 'string')
            .map((docKind) =>
              mapContextspaceDocument({
                kind: docKind,
                name: contextspaceFilename(docKind),
                content: payload[docKind],
                is_pinned: true
              })
            )
      )
  };

  settings = {
    getSession: async (): Promise<ApiResult<JsonRecord>> => this.getJson<JsonRecord>('/api/session/settings'),
    updateSession: async (body: unknown): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/api/session/settings', { method: 'POST', body })
  };

  voice = {
    getConfig: async (): Promise<ApiResult<JsonRecord>> => this.getJson<JsonRecord>('/api/voice/config'),
    transcribe: async (audio: Blob, filename = 'voice.webm'): Promise<ApiResult<JsonRecord>> => {
      const form = new FormData();
      form.append('file', audio, filename);
      return this.uploadForm<JsonRecord>('/api/voice/transcribe', form);
    }
  };

}

export function mapResult<T, U>(result: ApiResult<T>, mapper: (data: T) => U): ApiResult<U> {
  if (!result.ok) return result;
  try {
    return { ok: true, data: mapper(result.data) };
  } catch (error) {
    return {
      ok: false,
      error: {
        ...normalizeApiError(error),
        kind: 'parse',
        code: 'mapper_failed',
        message: error instanceof Error ? error.message : 'Could not normalize server payload.'
      }
    };
  }
}

export function dataOr<T>(result: ApiResult<T>, fallback: T): T {
  return result.ok ? result.data : fallback;
}

export function partialPageIssue(
  id: string,
  title: string,
  error: ApiError,
  retryLabel = 'Retry'
): PartialPageIssue {
  return {
    id,
    title,
    message: error.message,
    retryLabel
  };
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asArray(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object') : [];
}

function mapPmaThreadQueue(raw: JsonRecord): PmaThreadQueue {
  return {
    managedThreadId: stringValue(raw.managed_thread_id ?? raw.managedThreadId, ''),
    queueDepth: numberValue(raw.queue_depth ?? raw.queueDepth, 0),
    queuedTurns: asArray(raw.queued_turns ?? raw.queuedTurns).map(mapPmaQueuedTurn)
  };
}

function mapPmaQueuedTurn(raw: JsonRecord): PmaQueuedTurn {
  return {
    managedTurnId: stringValue(raw.managed_turn_id ?? raw.managedTurnId, ''),
    position: numberValue(raw.position, 0),
    state: stringValue(raw.state, ''),
    prompt: stringValue(raw.prompt, ''),
    promptPreview: stringValue(raw.prompt_preview ?? raw.promptPreview ?? raw.prompt, ''),
    attachments: asArray(raw.attachments),
    model: nullableString(raw.model),
    reasoning: nullableString(raw.reasoning),
    enqueuedAt: nullableString(raw.enqueued_at ?? raw.enqueuedAt),
    raw
  };
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

function nullableString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function numberValue(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function isWorktreeItem(item: JsonRecord): boolean {
  return item.kind === 'worktree' || typeof item.worktree_of === 'string' || typeof item.base_repo_id === 'string';
}

function contextspaceApiPath(workspaceId?: string): string {
  const id = workspaceId?.trim();
  if (!id || id === 'local') return '/api/contextspace';
  return `/repos/${encodeURIComponent(id)}/api/contextspace`;
}

function contextspaceUpdateApiPath(workspaceId: string | undefined, kind: string): string {
  const encodedKind = encodeURIComponent(kind);
  const id = workspaceId?.trim();
  if (!id || id === 'local') return `/api/contextspace/${encodedKind}`;
  return `/repos/${encodeURIComponent(id)}/api/contextspace/${encodedKind}`;
}

function hubTicketPath(owner?: { repo?: string; worktree?: string }): string {
  const params = new URLSearchParams();
  if (owner?.repo) params.set('repo', owner.repo);
  if (owner?.worktree) params.set('worktree', owner.worktree);
  const query = params.toString();
  return query ? `/hub/tickets?${query}` : '/hub/tickets';
}

function legacyTicketPath(owner?: { repo?: string; worktree?: string }): string {
  const workspaceId = owner?.repo ?? owner?.worktree;
  if (workspaceId) return `/repos/${encodeURIComponent(workspaceId)}/api/flows/ticket_flow/tickets`;
  return '/api/flows/ticket_flow/tickets';
}

function flowRunsPath(owner?: { repo?: string; worktree?: string }): string {
  const workspaceId = owner?.repo ?? owner?.worktree;
  const path = workspaceId ? `/repos/${encodeURIComponent(workspaceId)}/api/flows/runs` : '/api/flows/runs';
  return `${path}?flow_type=ticket_flow`;
}

function flowRunPath(runId: string, suffix: string, owner?: { repo?: string; worktree?: string }): string {
  const workspaceId = owner?.repo ?? owner?.worktree;
  const basePath = workspaceId ? `/repos/${encodeURIComponent(workspaceId)}/api/flows` : '/api/flows';
  return `${basePath}/${encodeURIComponent(runId)}/${suffix}`;
}

function ticketWithFallbackOwner(ticket: JsonRecord, owner?: { repo?: string; worktree?: string }): JsonRecord {
  if (owner?.repo) {
    return {
      ...ticket,
      workspace_kind: ticket.workspace_kind ?? 'repo',
      workspace_id: ticket.workspace_id ?? owner.repo,
      repo_id: ticket.repo_id ?? owner.repo
    };
  }
  if (owner?.worktree) {
    return {
      ...ticket,
      workspace_kind: ticket.workspace_kind ?? 'worktree',
      workspace_id: ticket.workspace_id ?? owner.worktree,
      worktree_id: ticket.worktree_id ?? ticket.worktree_repo_id ?? owner.worktree
    };
  }
  return ticket;
}

function contextspaceFilename(kind: string): string {
  return `${kind}.md`;
}

function isStandardPmaDoc(name: string): boolean {
  return (
    name === 'AGENTS.md' ||
    name === 'active_context.md' ||
    name === 'context_log.md' ||
    name === 'ABOUT_CAR.md' ||
    name === 'prompt.md'
  );
}

export const pmaApi = new PmaApiClient();
