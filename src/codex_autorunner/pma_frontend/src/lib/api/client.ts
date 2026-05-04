import {
  mapContextspaceDocument,
  mapDashboardSummary,
  mapPmaChatMessage,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSensitiveApprovalRequest,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapTicketSummary,
  mapWorktreeSummary,
  type ContextspaceDocument,
  type DashboardSummary,
  type PmaChatMessage,
  type PmaChatSummary,
  type PmaRunProgress,
  type RepoSummary,
  type SensitiveApprovalRequest,
  type SurfaceArtifact,
  type TicketDetail,
  type TicketSummary,
  type WorktreeSummary
} from '$lib/viewModels/domain';

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

export type JsonRecord = Record<string, unknown>;

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
      if (text.trim()) message = text.trim();
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

export class PmaApiClient {
  constructor(
    private readonly fetcher: typeof fetch = fetch,
    private readonly basePath = ''
  ) {}

  async requestJson<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
    const headers = new Headers(defaultHeaders);
    if (options.body !== undefined) headers.set('content-type', 'application/json');
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));

    try {
      const response = await this.fetcher(`${this.basePath}${path}`, {
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

  pma = {
    listChats: async (): Promise<ApiResult<PmaChatSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/threads'), (payload) =>
        asArray(payload.threads).map(mapPmaChatSummary)
      ),
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
    getMessages: async (chatId: string): Promise<ApiResult<PmaChatMessage[]>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/turns`), (payload) =>
        asArray(payload.turns ?? payload.messages).map(mapPmaChatMessage)
      ),
    getTail: async (chatId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/tail`), mapPmaRunProgress),
    getStatus: async (chatId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/status`), mapPmaRunProgress),
    listFiles: async (): Promise<ApiResult<SurfaceArtifact[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/files'), (payload) =>
        [...asArray(payload.inbox), ...asArray(payload.outbox)].map(mapSurfaceArtifact)
      ),
    listAgents: async (): Promise<ApiResult<JsonRecord[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/agents'), (payload) => asArray(payload.agents)),
    listAgentModels: async (agentId: string): Promise<ApiResult<JsonRecord[]>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/agents/${encodeURIComponent(agentId)}/models`), (payload) =>
        asArray(payload.models)
      ),
    listDocs: async (): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/docs'), (payload) =>
        asArray(payload.docs ?? payload.documents).map(mapContextspaceDocument)
      )
  };

  hub = {
    getDashboard: async (): Promise<ApiResult<DashboardSummary>> =>
      mapResult(
        await this.getJson<JsonRecord>('/hub/messages?sections=inbox,pma_threads,pma_files_detail,automation,action_queue,freshness'),
        mapDashboardSummary
      ),
    listRepos: async (): Promise<ApiResult<RepoSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/repos'), (payload) =>
        asArray(payload.repos ?? payload.items).map(mapRepoSummary)
      ),
    listWorktrees: async (): Promise<ApiResult<WorktreeSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/repos'), (payload) =>
        asArray(payload.worktrees ?? payload.repos ?? payload.items).map(mapWorktreeSummary)
      )
  };

  ticketFlow = {
    listRuns: async (): Promise<ApiResult<PmaRunProgress[]>> =>
      mapResult(await this.getJson<JsonRecord[]>('/api/flows/runs?flow_type=ticket_flow'), (payload) =>
        payload.map(mapPmaRunProgress)
      ),
    getRun: async (runId: string): Promise<ApiResult<PmaRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/status`), mapPmaRunProgress),
    listTickets: async (): Promise<ApiResult<TicketSummary[]>> =>
      mapResult(await this.getJson<JsonRecord>('/api/ticket_flow/tickets'), (payload) =>
        asArray(payload.tickets).map(mapTicketSummary)
      ),
    getTicket: async (index: number): Promise<ApiResult<TicketDetail>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/ticket_flow/tickets/${encodeURIComponent(index)}`), mapTicketDetail),
    listArtifacts: async (runId: string): Promise<ApiResult<SurfaceArtifact[]>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/dispatch_history`), (payload) =>
        asArray(payload.history).flatMap((entry) => asArray(entry.attachments)).map(mapSurfaceArtifact)
      )
  };

  contextspace = {
    listDocuments: async (): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(await this.getJson<JsonRecord>('/api/contextspace'), (payload) =>
        ['active_context', 'decisions', 'spec']
          .filter((kind) => typeof payload[kind] === 'string')
          .map((kind) => mapContextspaceDocument({ kind, name: kind, content: payload[kind] }))
      ),
    updateDocument: async (kind: string, content: string): Promise<ApiResult<ContextspaceDocument[]>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/api/contextspace/${encodeURIComponent(kind)}`, {
          method: 'PUT',
          body: { content }
        }),
        (payload) =>
          ['active_context', 'decisions', 'spec']
            .filter((docKind) => typeof payload[docKind] === 'string')
            .map((docKind) => mapContextspaceDocument({ kind: docKind, name: docKind, content: payload[docKind] }))
      )
  };

  settings = {
    getSession: async (): Promise<ApiResult<JsonRecord>> => this.getJson<JsonRecord>('/api/session/settings'),
    updateSession: async (body: unknown): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/api/session/settings', { method: 'POST', body }),
    listApprovals: async (): Promise<ApiResult<SensitiveApprovalRequest[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/messages?sections=automation,action_queue'), (payload) =>
        [...asArray(payload.automation), ...asArray(payload.action_queue)].map(mapSensitiveApprovalRequest)
      )
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

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asArray(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object') : [];
}

export const pmaApi = new PmaApiClient();
