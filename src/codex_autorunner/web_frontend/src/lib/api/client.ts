import {
  mapReadModelContract,
  type PreviewServiceKind,
  type PreviewServiceReadModel,
  type PreviewServiceScopeLink,
  type PreviewServiceStatus,
  type PreviewServicesReadModel,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import {
  mapContextspaceDocument,
  mapArtifactDelivery,
  mapDashboardSummary,
  mapChatMessage,
  mapChatTimelineItem,
  mapChatSummary,
  mapChatRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapTicketSummary,
  mapWorktreeSummary,
  type ContextspaceDocument,
  type ArtifactDelivery,
  type DashboardSummary,
  type ChatMessage,
  type ChatSummary,
  type ChatRunProgress,
  type ChatTimelineItem,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketDetail,
  type TicketSummary,
  type WorktreeSummary
} from '$lib/viewModels/domain';
import { mapChatTranscriptSnapshot, type ChatTranscriptSnapshot } from '$lib/viewModels/chat';
import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';
import { hostedBearerToken } from '$lib/runtime/hostedAuth';

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

export type ChatTimelineRequest = {
  limit?: number;
};

export type ChatQueuedTurn = {
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

export type ChatThreadQueue = {
  managedThreadId: string;
  queueDepth: number;
  queuedTurns: ChatQueuedTurn[];
};

export type ChatBulkRetireResult = {
  threads: ChatSummary[];
  retiredCount: number;
  requestedCount: number;
  errorCount: number;
  errors: JsonRecord[];
};

export type FileBoxName = 'inbox' | 'outbox';

export type ChatFileBoxScope =
  | { kind: 'hub' }
  | { kind: 'repo'; repoId: string };

export type WorktreeRetireRequest = {
  worktreeRepoId: string;
  force?: boolean;
  forceAttestation?: string | null;
  forceRetire?: boolean;
  retireNote?: string | null;
};

export type WorktreeArchiveRequest = {
  worktreeRepoId: string;
  archived: boolean;
};

export type RepoStateRetireRequest = {
  kind: 'repo' | 'worktree';
  id: string;
  retireNote?: string | null;
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

export type HubState = {
  title: string;
};

export type SystemUpdateTargetOption = {
  value: string;
  label: string;
  description: string | null;
  includesWeb: boolean;
  restartNotice: string | null;
};

export type SystemUpdateTargets = {
  targets: SystemUpdateTargetOption[];
  defaultTarget: string;
};

export type SystemUpdateRequest = {
  target?: string | null;
  force?: boolean;
};

export type SystemUpdateResponse = {
  status: string;
  message: string;
  target: string;
  requiresConfirmation: boolean;
};

export type SystemUpdateStatus = {
  status: string;
  message: string;
  at: number | null;
  phase: string | null;
  errorType: string | null;
  exitCode: number | null;
  updateRunId: string | null;
  updateTarget: string | null;
  raw: JsonRecord;
};

export type PreviewServiceAction = 'start' | 'stop' | 'restart' | 'health' | 'kill' | 'teardown' | 'unlink';

export type PreviewServiceDestructiveRequest = {
  force?: boolean;
  forceAttestation?: string | null;
};

export type PreviewServiceLogs = {
  serviceId: string;
  tail: number;
  stderr?: boolean;
  since?: string | null;
  text: string;
  exitCode?: number | null;
  startedAt?: string | null;
  exitedAt?: string | null;
  lastExitReason?: string | null;
  events?: JsonRecord[];
};

export type PreviewServiceLink = {
  serviceId: string;
  previewUrl: string;
  expiresAt: number | null;
};

export type PreviewServiceRevokeLinkResult = {
  serviceId: string;
  revoked: number;
};

export type AutomationScheduleSummary = {
  scheduleId: string;
  scheduleKind: string;
  timezone: string;
  nextFireAt: string | null;
  lastFireAt: string | null;
  state: string;
  schedule: JsonRecord;
  raw: JsonRecord;
};

export type AutomationJobSummary = {
  jobId: string;
  state: string;
  rawState: string;
  effectiveState: string;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
  resultSummary: string | null;
  errorText: string | null;
  attemptCount: number;
  blockedByJobId: string | null;
  blockedReason: string | null;
  blockedAt: string | null;
  pmaQueueResult: JsonRecord | null;
  childExecution: JsonRecord | null;
  children: JsonRecord[];
  runtimeContract: JsonRecord | null;
  terminalReason: string | null;
  policyViolations: JsonRecord[];
  raw: JsonRecord;
};

export type AutomationProductProjection = {
  productApiVersion: number;
  editable: {
    canEnable: boolean;
    canRename: boolean;
    canEditSchedule: boolean;
    canEditMessage: boolean;
    canEditTicketBody: boolean;
    canRunNow: boolean;
    canEditRaw: boolean;
    rawEditBlockedReason: string;
    managedReason: string | null;
    raw: JsonRecord;
  };
  managed: {
    systemOwned: boolean;
    managed: boolean;
    reason: string | null;
    raw: JsonRecord;
  };
  scheduleEditor: {
    kind: string;
    editable: boolean;
    fields: JsonRecord;
    timezone: string | null;
    nextFireAt: string | null;
    lastFireAt: string | null;
    state: string;
    summary: string;
    raw: JsonRecord;
  };
  triggerSummary: {
    kind: string;
    label: string;
    eventTypes: string[];
    filters: JsonRecord;
    raw: JsonRecord;
  };
  message: {
    source: string;
    field: string | null;
    preview: string;
    template: boolean;
    editable: boolean;
    raw: JsonRecord;
  };
  messageSource: string;
  messagePreview: string;
  actionPreview: JsonRecord;
  targetSummary: JsonRecord;
  executorSummary: JsonRecord;
  policySummary: JsonRecord;
  diagnostics: JsonRecord[];
  rawLinks: JsonRecord;
};

export type AutomationPresetDescriptor = {
  id: 'security_scan_pr' | 'weekly_ticket_flow';
  name: string;
  kind: string;
  description: string;
  schedule: {
    kind: 'daily' | 'weekly';
    timezone: string;
    hour: number;
    minute: number;
    weekday: number | null;
    raw: JsonRecord;
  };
  targetPolicy: string;
  targetShape: JsonRecord;
  executorKind: string;
  executorShape: JsonRecord;
  policy: JsonRecord;
  promptTemplate: string;
  ticketBodyTemplate: string | null;
  raw: JsonRecord;
};

export type AutomationSummary = {
  id: string;
  name: string;
  enabled: boolean;
  systemOwned: boolean;
  kind: string;
  executorKind: string;
  targetPolicy: string;
  target: JsonRecord;
  metadata: JsonRecord;
  schedule: AutomationScheduleSummary | null;
  lastJob: AutomationJobSummary | null;
  jobs: AutomationJobSummary[];
  jobCount: number;
  createdAt: string | null;
  updatedAt: string | null;
  product: AutomationProductProjection;
  raw: JsonRecord;
};

export type AutomationOverview = {
  automations: AutomationSummary[];
  presets: AutomationPresetDescriptor[];
  summary: {
    total: number;
    active: number;
    paused: number;
    failedJobs: number;
  };
};

export type AutomationTargetOption = {
  id: string;
  label: string;
  kind: 'repo' | 'worktree';
  disabled: boolean;
  raw: JsonRecord;
};

export type AutomationAgentDefaults = {
  defaultAgent: string;
  defaultProfile: string | null;
  defaultModel: string | null;
  defaultReasoning: string | null;
  raw: JsonRecord;
};

export type AutomationWorkspace = AutomationOverview & {
  targetOptions: AutomationTargetOption[];
  agentDefaults: AutomationAgentDefaults;
  generatedAt: string | null;
};

export type AutomationCreateRequest = {
  preset: 'security_scan_pr' | 'weekly_ticket_flow';
  execution_mode?: string | null;
  name?: string | null;
  repo_id?: string | null;
  timezone?: string;
  hour?: number;
  minute?: number;
  weekday?: number;
  prompt?: string | null;
  ticket_body?: string | null;
  agent?: string | null;
  model?: string | null;
  reasoning?: string | null;
  profile?: string | null;
  worker_child_policy?: JsonRecord | null;
  enabled?: boolean;
};

export type AutomationUpdateRequest = {
  name?: string | null;
  enabled?: boolean;
  execution_mode?: string | null;
  timezone?: string;
  hour?: number;
  minute?: number;
  weekday?: number;
  prompt?: string | null;
  ticket_body?: string | null;
  agent?: string | null;
  model?: string | null;
  reasoning?: string | null;
  profile?: string | null;
  worker_child_policy?: JsonRecord | null;
  metadata?: JsonRecord;
};

export type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: unknown;
};

export type HubBearerTokenProvider = () => string | null | undefined;

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

function fileBoxRoute(scope: ChatFileBoxScope): string {
  if (scope.kind === 'repo') return `/hub/filebox/${encodeURIComponent(scope.repoId)}`;
  return '/hub/pma/files';
}

function isHubControlPath(path: string): boolean {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  if (cleanPath === '/hub' || cleanPath.startsWith('/hub/')) return true;
  if (cleanPath === '/api' || cleanPath.startsWith('/api/')) return true;
  const parts = cleanPath.split('/');
  return parts.length >= 4 && parts[1] === 'repos' && parts[3] === 'api';
}

export class WebApiClient {
  constructor(
    private readonly fetcher: typeof fetch = fetch,
    private readonly basePath = runtimeBasePath(),
    private readonly hubBearerTokenProvider?: HubBearerTokenProvider
  ) {}

  async requestJson<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
    const headers = new Headers(defaultHeaders);
    if (options.body !== undefined) headers.set('content-type', 'application/json');
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));
    this.attachHubBearer(path, headers);

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
    const headers = new Headers({ accept: 'application/json' });
    this.attachHubBearer(path, headers);
    try {
      const response = await this.fetcher(this.url(path), {
        method: 'POST',
        body,
        headers
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

  private attachHubBearer(path: string, headers: Headers): void {
    if (headers.has('authorization')) return;
    if (!isHubControlPath(path)) return;
    const token = this.hubBearerTokenProvider?.()?.trim();
    if (!token) return;
    headers.set('authorization', `Bearer ${token}`);
  }

  filebox = {
    listFiles: async (scope: ChatFileBoxScope = { kind: 'hub' }): Promise<ApiResult<SurfaceArtifact[]>> => {
      const route = fileBoxRoute(scope);
      return mapResult(await this.getJson<JsonRecord>(route), (payload) =>
        [...asArray(payload.inbox), ...asArray(payload.outbox)].map(mapSurfaceArtifact)
      );
    },
    deleteFile: async (scope: ChatFileBoxScope, box: FileBoxName, filename: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`${fileBoxRoute(scope)}/${encodeURIComponent(box)}/${encodeURIComponent(filename)}`, {
        method: 'DELETE'
      }),
    deleteBox: async (scope: ChatFileBoxScope, box: FileBoxName): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`${fileBoxRoute(scope)}/${encodeURIComponent(box)}`, { method: 'DELETE' })
  };

  pma = {
    // Legacy diagnostics/tests only. Screen routes use chat index/detail projections.
    listChats: async (status: 'active' | 'archived' | null = 'active'): Promise<ApiResult<ChatSummary[]>> => {
      const query = status ? `?status=${encodeURIComponent(status)}` : '';
      return mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads${query}`), (payload) =>
        asArray(payload.threads).map(mapChatSummary)
      );
    },
    createChat: async (body: unknown): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/threads', {
          method: 'POST',
          body
        }),
        (payload) => mapChatSummary(asRecord(payload.thread ?? payload))
      ),
    startChatWithMessage: async (body: unknown): Promise<ApiResult<ChatMessage>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/thread-starts', {
          method: 'POST',
          body
        }),
        (payload) => mapChatMessage(asRecord(payload.message ?? payload.turn ?? payload))
      ),
    getChat: async (chatId: string): Promise<ApiResult<ChatSummary>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}`), (payload) =>
        mapChatSummary(asRecord(payload.thread))
      ),
    renameChat: async (chatId: string, title: string): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/title`, {
          method: 'PATCH',
          body: { title }
        }),
        (payload) => mapChatSummary(asRecord(payload.thread ?? payload))
      ),
    sendMessage: async (chatId: string, body: unknown): Promise<ApiResult<ChatMessage>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/messages`, {
          method: 'POST',
          body
        }),
        (payload) => mapChatMessage(asRecord(payload.message ?? payload.turn ?? payload))
      ),
    forkThread: async (chatId: string, body: unknown): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/fork`, {
          method: 'POST',
          body
        }),
        (payload) => mapChatSummary(asRecord(payload.thread ?? payload))
      ),
    // Chat rendering uses transcript projections; timeline and tail are diagnostics-only.
    getTranscript: async (chatId: string, request: ChatTimelineRequest = {}): Promise<ApiResult<ChatTranscriptSnapshot>> => {
      const params = new URLSearchParams({ limit: String(request.limit ?? 200) });
      return mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/transcript?${params.toString()}`), (payload) =>
        mapChatTranscriptSnapshot(payload, mapChatRunProgress)
      );
    },
    diagnostics: {
      // Legacy diagnostics/tests only. Screen routes use chat index/detail projections.
      getTimeline: async (chatId: string, request: ChatTimelineRequest = {}): Promise<ApiResult<ChatTimelineItem[]>> => {
        const params = new URLSearchParams({ limit: String(request.limit ?? 50) });
        return mapResult(
          await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/timeline?${params.toString()}`),
          (payload) => asArray(payload.items).map(mapChatTimelineItem)
        );
      },
      getTail: async (chatId: string): Promise<ApiResult<ChatRunProgress>> =>
        mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/tail`), mapChatRunProgress)
    },
    getStatus: async (chatId: string): Promise<ApiResult<ChatRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/status`), mapChatRunProgress),
    getQueue: async (chatId: string): Promise<ApiResult<ChatThreadQueue>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/queue`), mapChatThreadQueue),
    interruptThread: async (chatId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/interrupt`, { method: 'POST' }),
    resumeThread: async (chatId: string): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/resume`, {
          method: 'POST',
          body: {}
        }),
        (payload) => mapChatSummary(asRecord(payload.thread))
      ),
    compactThread: async (chatId: string, summary: string): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/compact`, {
          method: 'POST',
          body: { summary, reset_backend: true }
        }),
        (payload) => mapChatSummary(asRecord(payload.thread))
      ),
    retireThread: async (chatId: string): Promise<ApiResult<ChatSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/pma/threads/${encodeURIComponent(chatId)}/retire`, {
          method: 'POST'
        }),
        (payload) => mapChatSummary(asRecord(payload.thread))
      ),
    retireThreads: async (chatIds: string[]): Promise<ApiResult<ChatBulkRetireResult>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/threads/retire', {
          method: 'POST',
          body: { thread_ids: chatIds }
        }),
        (payload) => ({
          threads: asArray(payload.threads).map(mapChatSummary),
          retiredCount: numberValue(payload.retired_count ?? payload.retiredCount, 0),
          requestedCount: numberValue(payload.requested_count ?? payload.requestedCount, chatIds.length),
          errorCount: numberValue(payload.error_count ?? payload.errorCount, 0),
          errors: asArray(payload.errors)
        })
      ),
    retireActiveThreads: async (): Promise<ApiResult<ChatBulkRetireResult>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/pma/threads/retire-active', {
          method: 'POST'
        }),
        (payload) => ({
          threads: asArray(payload.threads).map(mapChatSummary),
          retiredCount: numberValue(payload.retired_count ?? payload.retiredCount, 0),
          requestedCount: numberValue(payload.requested_count ?? payload.requestedCount, 0),
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
    listFiles: async (): Promise<ApiResult<SurfaceArtifact[]>> => this.filebox.listFiles({ kind: 'hub' }),
    listArtifactDeliveries: async (repoId?: string | null): Promise<ApiResult<ArtifactDelivery[]>> => {
      const route = repoId
        ? `/hub/filebox/${encodeURIComponent(repoId)}/artifacts/deliveries`
        : '/hub/artifacts/deliveries';
      return mapResult(await this.getJson<JsonRecord>(route), (payload) =>
        asArray(payload.deliveries).map(mapArtifactDelivery)
      );
    },
    uploadInboxFile: async (file: File): Promise<ApiResult<string[]>> => {
      const form = new FormData();
      form.append('file', file, file.name);
      return mapResult(await this.uploadForm<JsonRecord>('/hub/pma/files/inbox', form), (payload) =>
        Array.isArray(payload.saved) ? payload.saved.filter((name): name is string => typeof name === 'string') : []
      );
    },
    deleteFile: async (box: FileBoxName, filename: string): Promise<ApiResult<JsonRecord>> =>
      this.filebox.deleteFile({ kind: 'hub' }, box, filename),
    deleteFileBox: async (box: FileBoxName): Promise<ApiResult<JsonRecord>> =>
      this.filebox.deleteBox({ kind: 'hub' }, box),
    listAgents: async (): Promise<ApiResult<{ agents: JsonRecord[]; agentStatuses: JsonRecord[]; default: string; defaults: JsonRecord; setupPrompt: string }>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/pma/agents'), (payload) => ({
        agents: asArray(payload.agents),
        agentStatuses: asArray(payload.agent_statuses ?? payload.agentStatuses),
        default: typeof payload.default === 'string' ? payload.default : '',
        defaults: asRecord(payload.defaults),
        setupPrompt: typeof payload.setup_prompt === 'string' ? payload.setup_prompt : ''
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
    getState: async (): Promise<ApiResult<HubState>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/state'), mapHubState),
    updateState: async (request: Partial<HubState>): Promise<ApiResult<HubState>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/state', {
          method: 'PUT',
          body: request
        }),
        mapHubState
      ),
    getDashboard: async (): Promise<ApiResult<DashboardSummary>> =>
      mapResult(
        await this.getJson<JsonRecord>('/hub/messages?sections=inbox,managed_threads,pma_files_detail,automation,action_queue,freshness'),
        mapDashboardSummary
      ),
    getAutomationWorkspace: async (): Promise<ApiResult<AutomationWorkspace>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/read-models/automations/workspace'), mapAutomationWorkspace),
    getAutomationWorkspaceIndex: async (): Promise<ApiResult<AutomationWorkspace>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/read-models/automations/workspace-index'), mapAutomationWorkspace),
    getAutomationTargetOptions: async (): Promise<ApiResult<AutomationTargetOption[]>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/read-models/automations/target-options'), (payload) =>
        asArray(payload.target_options ?? payload.targetOptions).map(mapAutomationTargetOption)
      ),
    getServicesReadModel: async (scope?: string | null): Promise<ApiResult<PreviewServicesReadModel>> => {
      const params = new URLSearchParams();
      if (scope?.trim()) params.set('scope', scope.trim());
      const query = params.toString();
      return mapResult(await this.getJson<JsonRecord>(`/hub/read-models/services${query ? `?${query}` : ''}`), mapPreviewServicesReadModel);
    },
    getService: async (serviceId: string): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(await this.getJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}`), (payload) =>
        mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    registerStaticService: async (request: JsonRecord): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/services/static', {
          method: 'POST',
          body: request
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    registerLoopbackService: async (request: JsonRecord): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/services/loopback-url', {
          method: 'POST',
          body: request
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    registerManagedService: async (request: JsonRecord): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/services/managed', {
          method: 'POST',
          body: request
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    updateService: async (serviceId: string, request: JsonRecord): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}`, {
          method: 'PATCH',
          body: request
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    issueServiceLink: async (serviceId: string, ttlSeconds = 86400): Promise<ApiResult<PreviewServiceLink>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}/preview-token?ttl=${encodeURIComponent(String(ttlSeconds))}`, {
          method: 'POST'
        }),
        mapPreviewServiceLink
      ),
    revokeServiceLinks: async (serviceId: string): Promise<ApiResult<PreviewServiceRevokeLinkResult>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}/preview-token/revoke`, {
          method: 'POST'
        }),
        (payload) => ({
          serviceId: stringValue(payload.service_id ?? payload.serviceId, ''),
          revoked: numberValue(payload.revoked, 0)
        })
      ),
    serviceAction: async (
      serviceId: string,
      action: PreviewServiceAction,
      request: PreviewServiceDestructiveRequest = {}
    ): Promise<ApiResult<PreviewServiceReadModel>> => {
      const body =
        action === 'kill' || action === 'teardown' || action === 'unlink'
          ? {
              force: request.force ?? false,
              force_attestation: request.forceAttestation ?? null
            }
          : undefined;
      return mapResult(
        await this.requestJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}/${action}`, {
          method: 'POST',
          body
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      );
    },
    deleteService: async (
      serviceId: string,
      request: PreviewServiceDestructiveRequest = {}
    ): Promise<ApiResult<PreviewServiceReadModel>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}`, {
          method: 'DELETE',
          body: {
            force: request.force ?? false,
            force_attestation: request.forceAttestation ?? null
          }
        }),
        (payload) => mapPreviewServiceReadModel(asRecord(payload.read_model ?? payload.service ?? payload))
      ),
    getServiceLogs: async (serviceId: string, tail = 200): Promise<ApiResult<PreviewServiceLogs>> =>
      mapResult(
        await this.getJson<JsonRecord>(`/hub/services/${encodeURIComponent(serviceId)}/logs?tail=${encodeURIComponent(String(tail))}`),
        mapPreviewServiceLogs
      ),
    listAutomations: async (): Promise<ApiResult<AutomationOverview>> =>
      mapResult(await this.getJson<JsonRecord>('/hub/automations'), mapAutomationOverview),
    getAutomation: async (ruleId: string): Promise<ApiResult<AutomationSummary>> =>
      mapResult(
        await this.getJson<JsonRecord>(`/hub/automations/${encodeURIComponent(ruleId)}`),
        (payload) => mapAutomationSummary(asRecord(payload.automation ?? payload))
      ),
    createAutomation: async (request: AutomationCreateRequest): Promise<ApiResult<AutomationSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/hub/automations', {
          method: 'POST',
          body: request
        }),
        (payload) => mapAutomationSummary(asRecord(payload.automation ?? payload))
      ),
    updateAutomation: async (ruleId: string, request: AutomationUpdateRequest): Promise<ApiResult<AutomationSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/automations/${encodeURIComponent(ruleId)}`, {
          method: 'PATCH',
          body: request
        }),
        (payload) => mapAutomationSummary(asRecord(payload.automation ?? payload))
      ),
    runAutomation: async (ruleId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/automations/${encodeURIComponent(ruleId)}/run`, { method: 'POST' }),
    deleteAutomation: async (ruleId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/automations/${encodeURIComponent(ruleId)}`, { method: 'DELETE' }),
    setAutomationEnabled: async (ruleId: string, enabled: boolean): Promise<ApiResult<AutomationSummary>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/hub/automations/${encodeURIComponent(ruleId)}/enabled`, {
          method: 'POST',
          body: { enabled }
        }),
        (payload) => mapAutomationSummary(asRecord(payload.automation ?? payload))
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
    retireWorktree: async (request: WorktreeRetireRequest): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/hub/worktrees/retire', {
        method: 'POST',
        body: {
          worktreeRepoId: request.worktreeRepoId,
          force: request.force ?? false,
          forceAttestation: request.forceAttestation ?? null,
          forceRetire: request.forceRetire ?? false,
          retireNote: request.retireNote ?? null
        }
      }),
    archiveWorktree: async (request: WorktreeArchiveRequest): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>('/hub/worktrees/archive', {
        method: 'POST',
        body: {
          worktreeRepoId: request.worktreeRepoId,
          archived: request.archived
        }
      }),
    retireState: async (request: RepoStateRetireRequest): Promise<ApiResult<JsonRecord>> => {
      const path = request.kind === 'repo' ? '/hub/repos/retire-state' : '/hub/worktrees/retire-state';
      const idKey = request.kind === 'repo' ? 'repoId' : 'worktreeRepoId';
      return this.requestJson<JsonRecord>(path, {
        method: 'POST',
        body: {
          [idKey]: request.id,
          retireNote: request.retireNote ?? null
        }
      });
    },
    setRepoPinned: async (repoId: string, pinned: boolean): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/repos/${encodeURIComponent(repoId)}/pin`, {
        method: 'POST',
        body: { pinned }
      }),
    syncRepoMain: async (repoId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/repos/${encodeURIComponent(repoId)}/sync-main`, {
        method: 'POST'
      }),
    syncWorktree: async (worktreeId: string): Promise<ApiResult<JsonRecord>> =>
      this.requestJson<JsonRecord>(`/hub/worktrees/${encodeURIComponent(worktreeId)}/sync`, {
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
    repoDetail: async (
      repoId: string,
      options: { ticketLimit?: number; ticketCursor?: string | null } = {}
    ): Promise<ApiResult<RepoWorktreeDetailSnapshot>> => {
      const params = new URLSearchParams();
      if (options.ticketLimit !== undefined) {
        params.set('ticket_limit', String(options.ticketLimit));
      }
      if (options.ticketCursor) params.set('ticket_cursor', options.ticketCursor);
      const query = params.toString();
      return mapResult(
        await this.getJson<JsonRecord>(
          `/hub/read-models/repos/${encodeURIComponent(repoId)}/detail${query ? `?${query}` : ''}`
        ),
        (payload) => mapReadModelContract<RepoWorktreeDetailSnapshot>(payload)
      );
    },
    worktreeDetail: async (
      worktreeId: string,
      options: { ticketLimit?: number; ticketCursor?: string | null } = {}
    ): Promise<ApiResult<RepoWorktreeDetailSnapshot>> => {
      const params = new URLSearchParams();
      if (options.ticketLimit !== undefined) {
        params.set('ticket_limit', String(options.ticketLimit));
      }
      if (options.ticketCursor) params.set('ticket_cursor', options.ticketCursor);
      const query = params.toString();
      return mapResult(
        await this.getJson<JsonRecord>(
          `/hub/read-models/worktrees/${encodeURIComponent(worktreeId)}/detail${query ? `?${query}` : ''}`
        ),
        (payload) => mapReadModelContract<RepoWorktreeDetailSnapshot>(payload)
      );
    },
    ticketDetail: async (
      ticketId: string,
      owner: { kind: 'repo' | 'worktree'; id: string }
    ): Promise<ApiResult<TicketDetailSnapshot>> => {
      const params = new URLSearchParams({ owner_kind: owner.kind, owner_id: owner.id });
      return mapResult(
        await this.getJson<JsonRecord>(`/hub/read-models/tickets/${encodeURIComponent(ticketId)}?${params.toString()}`),
        (payload) => mapReadModelContract<TicketDetailSnapshot>(payload)
      );
    }
  };

  ticketFlow = {
    // Legacy diagnostics/tests only. Ticket and owner screens read scoped ticket/run projections.
    listRuns: async (owner?: { repo?: string; worktree?: string }): Promise<ApiResult<ChatRunProgress[]>> =>
      mapResult(await this.getJson<JsonRecord[]>(flowRunsPath(owner)), (payload) =>
        payload.map(mapChatRunProgress)
      ),
    getRun: async (runId: string): Promise<ApiResult<ChatRunProgress>> =>
      mapResult(await this.getJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/status`), mapChatRunProgress),
    // Legacy diagnostics/tests only. Ticket and owner screens read scoped ticket projections.
    listTickets: async (owner?: { repo?: string; worktree?: string }): Promise<ApiResult<TicketSummary[]>> => {
      const hubResult = await this.getJson<JsonRecord>(hubTicketPath(owner));
      if (hubResult.ok || hubResult.error.status !== 404) {
        return mapResult(hubResult, (payload) => asArray(payload.tickets).map(mapTicketSummary));
      }
      if (!owner) return this.listLegacyMountedTickets();
      const legacyResult = await this.getJson<JsonRecord>(ticketApiPath(owner));
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
        await this.requestJson<JsonRecord>(`${ticketApiPath(owner)}/${encodeURIComponent(index)}`, {
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
        await this.requestJson<JsonRecord>(ticketApiPath(owner), {
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
      this.requestJson<JsonRecord>(`${ticketApiPath(owner)}/reorder`, {
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
    resumeRun: async (runId: string): Promise<ApiResult<ChatRunProgress>> =>
      mapResult(
        await this.requestJson<JsonRecord>(`/api/flows/${encodeURIComponent(runId)}/resume`, {
          method: 'POST'
        }),
        mapChatRunProgress
      ),
    bootstrap: async (): Promise<ApiResult<ChatRunProgress>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/api/flows/ticket_flow/bootstrap', {
          method: 'POST',
          body: { once: false }
        }),
        mapChatRunProgress
      )
  };

  private async listLegacyMountedTickets(): Promise<ApiResult<TicketSummary[]>> {
    const topologyResult = await this.readModels.repoWorktreeTopology('all', 200);
    if (!topologyResult.ok) {
      const legacyResult = await this.getJson<JsonRecord>(ticketApiPath());
      return mapResult(legacyResult, (payload) => asArray(payload.tickets).map((ticket) => mapTicketSummary(ticketWithFallbackOwner(ticket))));
    }
    const owners = [
      ...topologyResult.data.repos.map((repo) => ({ repo: repo.repoId })),
      ...topologyResult.data.worktrees.map((worktree) => ({ worktree: worktree.worktreeId }))
    ];
    const results = await Promise.all(
      owners.map(async (owner) => ({
        owner,
        result: await this.getJson<JsonRecord>(ticketApiPath(owner))
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

  system = {
    getUpdateTargets: async (): Promise<ApiResult<SystemUpdateTargets>> =>
      mapResult(await this.getJson<JsonRecord>('/system/update/targets'), mapSystemUpdateTargets),
    getUpdateStatus: async (): Promise<ApiResult<SystemUpdateStatus>> =>
      mapResult(await this.getJson<JsonRecord>('/system/update/status'), mapSystemUpdateStatus),
    startUpdate: async (request: SystemUpdateRequest): Promise<ApiResult<SystemUpdateResponse>> =>
      mapResult(
        await this.requestJson<JsonRecord>('/system/update', {
          method: 'POST',
          body: {
            target: request.target ?? null,
            force: request.force ?? false
          }
        }),
        mapSystemUpdateResponse
      )
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

function mapSystemUpdateTargets(raw: JsonRecord): SystemUpdateTargets {
  return {
    targets: asArray(raw.targets).map((target) => {
      const record = asRecord(target);
      return {
        value: stringValue(record.value, ''),
        label: stringValue(record.label, stringValue(record.value, 'update')),
        description: nullableString(record.description),
        includesWeb: Boolean(record.includes_web ?? record.includesWeb),
        restartNotice: nullableString(record.restart_notice ?? record.restartNotice)
      };
    }),
    defaultTarget: stringValue(raw.default_target ?? raw.defaultTarget, 'all')
  };
}

function mapSystemUpdateResponse(raw: JsonRecord): SystemUpdateResponse {
  return {
    status: stringValue(raw.status, 'unknown'),
    message: stringValue(raw.message, 'Update request returned without a message.'),
    target: stringValue(raw.target, ''),
    requiresConfirmation: Boolean(raw.requires_confirmation ?? raw.requiresConfirmation)
  };
}

function mapSystemUpdateStatus(raw: JsonRecord): SystemUpdateStatus {
  return {
    status: stringValue(raw.status, 'unknown'),
    message: stringValue(raw.message, 'No update status recorded.'),
    at: nullableNumber(raw.at),
    phase: nullableString(raw.phase),
    errorType: nullableString(raw.error_type ?? raw.errorType),
    exitCode: nullableNumber(raw.exit_code ?? raw.exitCode),
    updateRunId: nullableString(raw.update_run_id ?? raw.updateRunId),
    updateTarget: nullableString(raw.update_target ?? raw.updateTarget),
    raw
  };
}

function mapPreviewServicesReadModel(raw: JsonRecord): PreviewServicesReadModel {
  const counts = asRecord(raw.counts);
  return {
    services: asArray(raw.services).map(mapPreviewServiceReadModel),
    counts: {
      total: numberValue(counts.total, 0),
      running: numberValue(counts.running, 0),
      attention: numberValue(counts.attention, 0),
      managed: numberValue(counts.managed, 0),
      static: numberValue(counts.static, 0),
      loopback: numberValue(counts.loopback, 0),
      preview: numberValue(counts.preview, 0),
      application: numberValue(counts.application, 0),
      infrastructure: numberValue(counts.infrastructure, 0)
    }
  };
}

function mapPreviewServiceReadModel(raw: JsonRecord): PreviewServiceReadModel {
  return {
    serviceId: stringValue(raw.service_id ?? raw.serviceId, ''),
    name: stringValue(raw.name, 'Untitled service'),
    kind: previewServiceKind(raw.kind),
    serviceClass: previewServiceClass(raw.service_class ?? raw.serviceClass),
    trustLevel: previewServiceTrustLevel(raw.trust_level ?? raw.trustLevel),
    ownership: previewServiceOwnership(raw.ownership),
    networkPolicy: previewServiceNetworkPolicy(raw.network_policy ?? raw.networkPolicy),
    status: previewServiceStatus(raw.status),
    createdBy: nullableString(raw.created_by ?? raw.createdBy),
    createdAt: nullableString(raw.created_at ?? raw.createdAt),
    updatedAt: nullableString(raw.updated_at ?? raw.updatedAt),
    scopeLinks: asArray(raw.scope_links ?? raw.scopeLinks).map(mapPreviewServiceScopeLink),
    scope: nullableString(raw.scope),
    carUrl: stringValue(raw.car_url ?? raw.carUrl, ''),
    previewUrl: nullableString(raw.preview_url ?? raw.previewUrl),
    previewUrlExpiresAt: nullableNumber(raw.preview_url_expires_at ?? raw.previewUrlExpiresAt),
    proxyEnabled: raw.proxy_enabled ?? raw.proxyEnabled ?? true ? true : false,
    directUrl: nullableString(raw.direct_url ?? raw.directUrl),
    host: nullableString(raw.host),
    port: nullableNumber(raw.port),
    ownerPid: nullableNumber(raw.owner_pid ?? raw.ownerPid),
    healthCheck: recordOrNull(raw.health_check ?? raw.healthCheck),
    restartPolicy: asRecord(raw.restart_policy ?? raw.restartPolicy),
    logs: recordOrNull(raw.logs),
    metadata: asRecord(raw.metadata),
    capabilities: booleanRecord(raw.capabilities),
    desiredState: asRecord(raw.desired_state ?? raw.desiredState),
    observedState: asRecord(raw.observed_state ?? raw.observedState),
    raw
  };
}

function mapPreviewServiceScopeLink(raw: JsonRecord): PreviewServiceScopeLink {
  return {
    kind: stringValue(raw.kind, 'hub'),
    id: nullableString(raw.id),
    path: nullableString(raw.path)
  };
}

function mapPreviewServiceLogs(raw: JsonRecord): PreviewServiceLogs {
  return {
    serviceId: stringValue(raw.service_id ?? raw.serviceId, ''),
    tail: numberValue(raw.tail, 0),
    stderr: raw.stderr === true,
    since: nullableString(raw.since),
    text: stringValue(raw.text, ''),
    exitCode: nullableNumber(raw.exit_code ?? raw.exitCode),
    startedAt: nullableString(raw.started_at ?? raw.startedAt),
    exitedAt: nullableString(raw.exited_at ?? raw.exitedAt),
    lastExitReason: nullableString(raw.last_exit_reason ?? raw.lastExitReason),
    events: asArray(raw.events).map(asRecord)
  };
}

function mapPreviewServiceLink(raw: JsonRecord): PreviewServiceLink {
  return {
    serviceId: stringValue(raw.service_id ?? raw.serviceId, ''),
    previewUrl: stringValue(raw.preview_url ?? raw.previewUrl, ''),
    expiresAt: nullableNumber(raw.expires_at ?? raw.expiresAt)
  };
}

function previewServiceKind(value: unknown): PreviewServiceKind {
  const raw = stringValue(value, 'loopback_url');
  if (raw === 'static_file' || raw === 'static_dir' || raw === 'loopback_url' || raw === 'managed_command') return raw;
  return 'loopback_url';
}

function previewServiceClass(value: unknown): PreviewServiceReadModel['serviceClass'] {
  const raw = stringValue(value, 'preview');
  if (raw === 'preview' || raw === 'application' || raw === 'infrastructure') return raw;
  return 'preview';
}

function previewServiceTrustLevel(value: unknown): PreviewServiceReadModel['trustLevel'] {
  const raw = stringValue(value, 'generated');
  if (raw === 'trusted' || raw === 'generated' || raw === 'external') return raw;
  return 'generated';
}

function previewServiceOwnership(value: unknown): PreviewServiceReadModel['ownership'] {
  const raw = stringValue(value, 'external');
  if (raw === 'static' || raw === 'car_managed' || raw === 'external') return raw;
  return 'external';
}

function previewServiceNetworkPolicy(value: unknown): PreviewServiceReadModel['networkPolicy'] {
  const raw = stringValue(value, 'loopback_only');
  if (raw === 'loopback_only' || raw === 'internal_allowlist' || raw === 'explicit_allowlist' || raw === 'workspace_runtime') return raw;
  return 'loopback_only';
}

function previewServiceStatus(value: unknown): PreviewServiceStatus {
  const raw = stringValue(value, 'registered');
  const allowed = new Set([
    'registered',
    'starting',
    'running',
    'healthy',
    'unhealthy',
    'stopped',
    'exited',
    'failed',
    'orphaned',
    'conflict'
  ]);
  return allowed.has(raw) ? (raw as PreviewServiceStatus) : 'registered';
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

function booleanRecord(value: unknown): Record<string, boolean> {
  const raw = asRecord(value);
  const mapped: Record<string, boolean> = {};
  for (const [key, item] of Object.entries(raw)) {
    mapped[key] = item === true;
  }
  return mapped;
}

function recordOrNull(value: unknown): JsonRecord | null {
  const record = asRecord(value);
  return Object.keys(record).length ? record : null;
}

function asArray(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object') : [];
}

function mapChatThreadQueue(raw: JsonRecord): ChatThreadQueue {
  return {
    managedThreadId: stringValue(raw.managed_thread_id ?? raw.managedThreadId, ''),
    queueDepth: numberValue(raw.queue_depth ?? raw.queueDepth, 0),
    queuedTurns: asArray(raw.queued_turns ?? raw.queuedTurns).map(mapChatQueuedTurn)
  };
}

function mapChatQueuedTurn(raw: JsonRecord): ChatQueuedTurn {
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

function mapHubState(raw: JsonRecord): HubState {
  return {
    title: stringValue(raw.title, 'Web Hub').trim() || 'Web Hub'
  };
}

function mapAutomationOverview(raw: JsonRecord): AutomationOverview {
  const summary = asRecord(raw.summary);
  return {
    automations: asArray(raw.automations).map(mapAutomationSummary),
    presets: asArray(raw.presets).map(mapAutomationPresetDescriptor),
    summary: {
      total: numberValue(summary.total, 0),
      active: numberValue(summary.active, 0),
      paused: numberValue(summary.paused, 0),
      failedJobs: numberValue(summary.failed_jobs ?? summary.failedJobs, 0)
    }
  };
}

function mapAutomationWorkspace(raw: JsonRecord): AutomationWorkspace {
  return {
    ...mapAutomationOverview(raw),
    targetOptions: asArray(raw.target_options ?? raw.targetOptions).map(mapAutomationTargetOption),
    agentDefaults: mapAutomationAgentDefaults(asRecord(raw.agent_defaults ?? raw.agentDefaults)),
    generatedAt: nullableString(raw.generated_at ?? raw.generatedAt)
  };
}

function mapAutomationTargetOption(raw: JsonRecord): AutomationTargetOption {
  const rawKind = stringValue(raw.kind, 'repo');
  return {
    id: stringValue(raw.id, ''),
    label: stringValue(raw.label, stringValue(raw.id, '')),
    kind: rawKind === 'worktree' ? 'worktree' : 'repo',
    disabled: Boolean(raw.disabled),
    raw
  };
}

function mapAutomationAgentDefaults(raw: JsonRecord): AutomationAgentDefaults {
  return {
    defaultAgent: stringValue(raw.default_agent ?? raw.defaultAgent, ''),
    defaultProfile: nullableString(raw.default_profile ?? raw.defaultProfile),
    defaultModel: nullableString(raw.default_model ?? raw.defaultModel),
    defaultReasoning: nullableString(raw.default_reasoning ?? raw.defaultReasoning),
    raw
  };
}

function mapAutomationPresetDescriptor(raw: JsonRecord): AutomationPresetDescriptor {
  const schedule = asRecord(raw.schedule);
  const rawId = stringValue(raw.id, 'security_scan_pr');
  const id = rawId === 'weekly_ticket_flow' ? 'weekly_ticket_flow' : 'security_scan_pr';
  const rawKind = stringValue(schedule.kind, 'daily');
  const scheduleKind = rawKind === 'weekly' ? 'weekly' : 'daily';
  return {
    id,
    name: stringValue(raw.name, id),
    kind: stringValue(raw.kind, id),
    description: stringValue(raw.description, ''),
    schedule: {
      kind: scheduleKind,
      timezone: stringValue(schedule.timezone, 'UTC'),
      hour: numberValue(schedule.hour, 9),
      minute: numberValue(schedule.minute, 0),
      weekday: schedule.weekday === null || schedule.weekday === undefined ? null : numberValue(schedule.weekday, 0),
      raw: schedule
    },
    targetPolicy: stringValue(raw.target_policy ?? raw.targetPolicy, ''),
    targetShape: asRecord(raw.target_shape ?? raw.targetShape),
    executorKind: stringValue(raw.executor_kind ?? raw.executorKind, ''),
    executorShape: asRecord(raw.executor_shape ?? raw.executorShape),
    policy: asRecord(raw.policy),
    promptTemplate: stringValue(raw.prompt_template ?? raw.promptTemplate, ''),
    ticketBodyTemplate: nullableString(raw.ticket_body_template ?? raw.ticketBodyTemplate),
    raw
  };
}

function mapAutomationSummary(raw: JsonRecord): AutomationSummary {
  const schedule = asRecord(raw.schedule);
  const lastJob = asRecord(raw.last_job ?? raw.lastJob);
  return {
    id: stringValue(raw.id, ''),
    name: stringValue(raw.name, 'Untitled automation'),
    enabled: Boolean(raw.enabled),
    systemOwned: Boolean(raw.system_owned ?? raw.systemOwned),
    kind: stringValue(raw.kind, ''),
    executorKind: stringValue(raw.executor_kind ?? raw.executorKind, ''),
    targetPolicy: stringValue(raw.target_policy ?? raw.targetPolicy, ''),
    target: asRecord(raw.target),
    metadata: asRecord(raw.metadata),
    schedule: Object.keys(schedule).length ? mapAutomationSchedule(schedule) : null,
    lastJob: Object.keys(lastJob).length ? mapAutomationJob(lastJob) : null,
    jobs: asArray(raw.jobs).map(mapAutomationJob),
    jobCount: numberValue(raw.job_count ?? raw.jobCount, 0),
    createdAt: nullableString(raw.created_at ?? raw.createdAt),
    updatedAt: nullableString(raw.updated_at ?? raw.updatedAt),
    product: mapAutomationProductProjection(raw),
    raw
  };
}

function mapAutomationProductProjection(raw: JsonRecord): AutomationProductProjection {
  const editable = asRecord(raw.editable);
  const managed = asRecord(raw.managed ?? raw.managed_status ?? raw.managedStatus);
  const scheduleEditor = asRecord(raw.schedule_editor ?? raw.scheduleEditor);
  const triggerSummary = asRecord(raw.trigger_summary ?? raw.triggerSummary);
  const message = asRecord(raw.message);
  return {
    productApiVersion: numberValue(raw.product_api_version ?? raw.productApiVersion, 0),
    editable: {
      canEnable: Boolean(editable.can_enable ?? editable.canEnable),
      canRename: Boolean(editable.can_rename ?? editable.canRename),
      canEditSchedule: Boolean(editable.can_edit_schedule ?? editable.canEditSchedule),
      canEditMessage: Boolean(editable.can_edit_message ?? editable.canEditMessage),
      canEditTicketBody: Boolean(editable.can_edit_ticket_body ?? editable.canEditTicketBody),
      canRunNow: Boolean(editable.can_run_now ?? editable.canRunNow),
      canEditRaw: Boolean(editable.can_edit_raw ?? editable.canEditRaw),
      rawEditBlockedReason: stringValue(editable.raw_edit_blocked_reason ?? editable.rawEditBlockedReason, ''),
      managedReason: nullableString(editable.managed_reason ?? editable.managedReason),
      raw: editable
    },
    managed: {
      systemOwned: Boolean(managed.system_owned ?? managed.systemOwned),
      managed: Boolean(managed.managed),
      reason: nullableString(managed.reason),
      raw: managed
    },
    scheduleEditor: {
      kind: stringValue(scheduleEditor.kind, ''),
      editable: Boolean(scheduleEditor.editable),
      fields: asRecord(scheduleEditor.fields),
      timezone: nullableString(scheduleEditor.timezone),
      nextFireAt: nullableString(scheduleEditor.next_fire_at ?? scheduleEditor.nextFireAt),
      lastFireAt: nullableString(scheduleEditor.last_fire_at ?? scheduleEditor.lastFireAt),
      state: stringValue(scheduleEditor.state, ''),
      summary: stringValue(scheduleEditor.summary, ''),
      raw: scheduleEditor
    },
    triggerSummary: {
      kind: stringValue(triggerSummary.kind, ''),
      label: stringValue(triggerSummary.label, ''),
      eventTypes: asArray(triggerSummary.event_types ?? triggerSummary.eventTypes).map((item) => String(item)),
      filters: asRecord(triggerSummary.filters),
      raw: triggerSummary
    },
    message: {
      source: stringValue(message.source, ''),
      field: nullableString(message.field),
      preview: stringValue(message.preview, ''),
      template: Boolean(message.template),
      editable: Boolean(message.editable),
      raw: message
    },
    messageSource: stringValue(raw.message_source ?? raw.messageSource, ''),
    messagePreview: stringValue(raw.message_preview ?? raw.messagePreview, ''),
    actionPreview: asRecord(raw.action_preview ?? raw.actionPreview),
    targetSummary: asRecord(raw.target_summary ?? raw.targetSummary),
    executorSummary: asRecord(raw.executor_summary ?? raw.executorSummary),
    policySummary: asRecord(raw.policy_summary ?? raw.policySummary),
    diagnostics: asArray(raw.diagnostics).map(asRecord),
    rawLinks: asRecord(raw.raw_links ?? raw.rawLinks)
  };
}

function mapAutomationSchedule(raw: JsonRecord): AutomationScheduleSummary {
  return {
    scheduleId: stringValue(raw.schedule_id ?? raw.scheduleId, ''),
    scheduleKind: stringValue(raw.schedule_kind ?? raw.scheduleKind, ''),
    timezone: stringValue(raw.timezone, 'UTC'),
    nextFireAt: nullableString(raw.next_fire_at ?? raw.nextFireAt),
    lastFireAt: nullableString(raw.last_fire_at ?? raw.lastFireAt),
    state: stringValue(raw.state, ''),
    schedule: asRecord(raw.schedule),
    raw
  };
}

function mapAutomationJob(raw: JsonRecord): AutomationJobSummary {
  return {
    jobId: stringValue(raw.job_id ?? raw.jobId, ''),
    state: stringValue(raw.state, ''),
    rawState: stringValue(raw.raw_state ?? raw.rawState ?? raw.state, ''),
    effectiveState: stringValue(raw.effective_state ?? raw.effectiveState ?? raw.state, ''),
    createdAt: nullableString(raw.created_at ?? raw.createdAt),
    startedAt: nullableString(raw.started_at ?? raw.startedAt),
    finishedAt: nullableString(raw.finished_at ?? raw.finishedAt),
    updatedAt: nullableString(raw.updated_at ?? raw.updatedAt),
    resultSummary: nullableString(raw.result_summary ?? raw.resultSummary),
    errorText: nullableString(raw.error_text ?? raw.errorText),
    attemptCount: numberValue(raw.attempt_count ?? raw.attemptCount, 0),
    blockedByJobId: nullableString(raw.blocked_by_job_id ?? raw.blockedByJobId),
    blockedReason: nullableString(raw.blocked_reason ?? raw.blockedReason),
    blockedAt: nullableString(raw.blocked_at ?? raw.blockedAt),
    pmaQueueResult: Object.keys(asRecord(raw.pma_queue_result ?? raw.pmaQueueResult)).length
      ? asRecord(raw.pma_queue_result ?? raw.pmaQueueResult)
      : null,
    childExecution: Object.keys(asRecord(raw.child_execution ?? raw.childExecution)).length
      ? asRecord(raw.child_execution ?? raw.childExecution)
      : null,
    children: asArray(raw.children).map(asRecord),
    runtimeContract: Object.keys(asRecord(raw.runtime_contract ?? raw.runtimeContract)).length
      ? asRecord(raw.runtime_contract ?? raw.runtimeContract)
      : null,
    terminalReason: nullableString(raw.terminal_reason ?? raw.terminalReason),
    policyViolations: asArray(raw.policy_violations ?? raw.policyViolations).map(asRecord),
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

function nullableNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
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

function ticketApiPath(owner?: { repo?: string; worktree?: string }): string {
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

export const webApi = new WebApiClient(fetch, runtimeBasePath(), hostedBearerToken);
