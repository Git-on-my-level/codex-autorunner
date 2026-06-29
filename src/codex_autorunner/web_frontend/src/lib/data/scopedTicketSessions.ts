import {
  dataOr,
  partialPageIssue,
  type ApiError,
  type ApiResult,
  type JsonRecord,
  type PartialPageIssue
} from '$lib/api/client';
import type { RepoWorktreeDetailSnapshot, TicketDetailSnapshot } from '$lib/api/readModelContracts';
import {
  mapChatSummary,
  mapChatRunProgress,
  mapTicketDetail,
  mapTicketSummary,
  type ChatSummary,
  type ChatRunProgress,
  type SurfaceArtifact,
  type TicketDetail,
  type TicketSummary
} from '$lib/viewModels/domain';
import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';
import {
  loadScopedActionManifest,
  scopedTicketQueueOwner,
  scopedTicketQueueScope,
  type ScopedTicketQueueConfig,
  type ScopedTicketQueueOwner
} from '$lib/viewModels/scopedTicketQueue';
import {
  buildTicketDetailViewModel,
  resolveTicketRouteId,
  ticketDetailFromSummary,
  type SurfaceActionManifest,
  type TicketHandoff,
  type TicketDetailViewModel,
  type TicketOwnerScope
} from '$lib/viewModels/ticket';
import { cachedTickets, rememberTickets } from '$lib/viewModels/ticketCache';
import { readModelEntityStore, type ReadModelEntityState, type ReadModelEntityStore } from './readModelStore';
import { scopedOwnerKey, selectChatRuns, selectTicketSummaries } from './readModelViewModels';

export type ScopedTicketSessionApi = {
  requestJson<T>(path: string, options?: unknown): Promise<ApiResult<T>>;
  readModels: {
    repoDetail(
      repoId: string,
      options?: { ticketLimit?: number; ticketCursor?: string | null }
    ): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
    worktreeDetail(
      worktreeId: string,
      options?: { ticketLimit?: number; ticketCursor?: string | null }
    ): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
    ticketDetail(
      ticketId: string,
      owner: { kind: 'repo' | 'worktree'; id: string }
    ): Promise<ApiResult<TicketDetailSnapshot>>;
  };
};

export type ScopedTicketListSession =
  | {
      ok: true;
      owner: ScopedTicketQueueOwner;
      ownerScope: Exclude<TicketOwnerScope, null>;
      ownerKey: string;
      tickets: TicketSummary[];
      runs: ChatRunProgress[];
      actionManifest: SurfaceActionManifest | null;
      handoff: TicketHandoff | null;
      sectionIssues: PartialPageIssue[];
      parentRepoId: string | null;
      redirectTo: string | null;
    }
  | { ok: false; error: ApiError };

export type ScopedTicketCachedDetail = {
  detail: TicketDetailViewModel;
  ownerKey: string;
};

export type ScopedTicketDetailSession =
  | {
      ok: true;
      ownerScope: Exclude<TicketOwnerScope, null>;
      ownerKey: string;
      owner: ScopedTicketQueueOwner;
      ticketDetail: TicketDetail;
      tickets: TicketSummary[];
      runs: ChatRunProgress[];
      chats: ChatSummary[];
      dispatches: JsonRecord[];
      detail: TicketDetailViewModel;
      currentRunId: string | null;
      sectionIssues: PartialPageIssue[];
      parentRepoId: string | null;
      redirectTo: string | null;
    }
  | { ok: false; error: ApiError };

export async function loadScopedTicketListSession(
  api: ScopedTicketSessionApi,
  config: ScopedTicketQueueConfig,
  options: {
    currentPath?: string | null;
    store?: ReadModelEntityStore;
  } = {}
): Promise<ScopedTicketListSession> {
  const detail = await loadCompleteScopedTicketDetail(api, config);
  if (!detail.ok) return { ok: false, error: detail.error };

  const parentRepoId = parentRepoIdFromSnapshot(detail.data);
  const ownerScope = scopedTicketQueueScope({ ...config, parentRepoId: config.parentRepoId ?? parentRepoId });
  const ownerKey = scopedOwnerKey(ownerScope);
  const owner = scopedTicketQueueOwner(config);
  const redirectTo =
    config.kind === 'worktree' && options.currentPath
      ? legacyWorktreeRedirectPath(options.currentPath, config.resourceId, parentRepoId)
      : null;
  const tickets = detail.data.ticketQueue.map(mapTicketSummary);
  const runs = detail.data.runQueue.map(mapChatRunProgress);
  hydrateScopedTicketQueue(options.store ?? readModelEntityStore, ownerKey, owner, tickets, runs);

  const manifest = await loadScopedActionManifest(api, { ...config, parentRepoId: config.parentRepoId ?? parentRepoId });
  const actionManifest = dataOr(manifest, null);
  const activeMessage = await api.requestJson<JsonRecord>(`${config.apiBasePath.replace(/\/api\/flows$/, '/api/messages')}/active`);
  const handoff = activeMessage.ok ? handoffFromActiveMessage(activeMessage.data) : null;
  const sectionIssues = [
    !manifest.ok ? partialPageIssue('action_manifest', 'Action manifest unavailable', manifest.error) : null,
    !activeMessage.ok ? partialPageIssue('active_dispatch', 'Paused dispatch unavailable', activeMessage.error) : null
  ].filter((issue): issue is PartialPageIssue => Boolean(issue));

  return {
    ok: true,
    owner,
    ownerScope,
    ownerKey,
    tickets,
    runs,
    actionManifest,
    handoff,
    sectionIssues,
    parentRepoId,
    redirectTo
  };
}

function handoffFromActiveMessage(payload: JsonRecord): TicketHandoff | null {
  if (payload.active !== true) return null;
  const dispatch = recordValue(payload.dispatch);
  if (!dispatch || dispatch.is_handoff !== true) return null;
  const runId = stringValue(payload.run_id);
  if (!runId) return null;
  return {
    runId,
    seq: numberValue(payload.seq),
    title: stringValue(dispatch.title) ?? 'Agent needs input',
    body: stringValue(dispatch.body) ?? '',
    isHandoff: true
  };
}

async function loadCompleteScopedTicketDetail(
  api: ScopedTicketSessionApi,
  config: ScopedTicketQueueConfig
): Promise<ApiResult<RepoWorktreeDetailSnapshot>> {
  const first =
    config.kind === 'repo'
      ? await api.readModels.repoDetail(config.resourceId)
      : await api.readModels.worktreeDetail(config.resourceId);
  if (!first.ok) return first;

  const ticketQueue = [...first.data.ticketQueue];
  const seenCursors = new Set<string>();
  let nextCursor = first.data.ticketWindow.nextCursor ?? null;
  let lastWindow = first.data.ticketWindow;
  while (nextCursor && !seenCursors.has(nextCursor)) {
    seenCursors.add(nextCursor);
    const page =
      config.kind === 'repo'
        ? await api.readModels.repoDetail(config.resourceId, { ticketCursor: nextCursor })
        : await api.readModels.worktreeDetail(config.resourceId, { ticketCursor: nextCursor });
    if (!page.ok) return page;
    ticketQueue.push(...page.data.ticketQueue);
    lastWindow = page.data.ticketWindow;
    nextCursor = page.data.ticketWindow.nextCursor ?? null;
  }

  return {
    ok: true,
    data: {
      ...first.data,
      ticketQueue,
      ticketWindow: {
        ...lastWindow,
        previousCursor: first.data.ticketWindow.previousCursor ?? null,
        nextCursor: null,
        totalEstimate: Math.max(
          lastWindow.totalEstimate ?? ticketQueue.length,
          ticketQueue.length
        ),
        totalIsExact: lastWindow.totalIsExact
      }
    }
  };
}

export function renderScopedTicketCachedDetail(
  ownerScope: Exclude<TicketOwnerScope, null>,
  routeTicketId: string,
  options: { store?: ReadModelEntityStore; readModelState?: ReadModelEntityState } = {}
): ScopedTicketCachedDetail | null {
  const store = options.store ?? readModelEntityStore;
  const owner = ownerFromScope(ownerScope);
  const ownerKey = scopedOwnerKey(ownerScope);
  const cachedList = cachedTickets(owner);
  if (cachedList) {
    const cached = renderTicketSummaryDetail(cachedList, ownerKey, routeTicketId, store);
    if (cached) return cached;
  }
  const state = options.readModelState ?? store.snapshot();
  const ticketList = selectTicketSummaries(state, ownerKey);
  return renderTicketSummaryDetail(ticketList, ownerKey, routeTicketId, store);
}

export async function loadScopedTicketDetailSession(
  api: ScopedTicketSessionApi,
  ownerScope: Exclude<TicketOwnerScope, null>,
  routeTicketId: string,
  options: {
    currentPath?: string | null;
    store?: ReadModelEntityStore;
    readModelState?: ReadModelEntityState;
  } = {}
): Promise<ScopedTicketDetailSession> {
  const store = options.store ?? readModelEntityStore;
  const snapshot = await api.readModels.ticketDetail(routeTicketId, { kind: ownerScope.kind, id: ownerScope.id });
  if (!snapshot.ok) return { ok: false, error: snapshot.error };

  const ticketRecord = snapshot.data.ticketDetail as JsonRecord;
  const parentRepoId =
    ownerScope.kind === 'worktree' ? stringValue(ticketRecord.base_repo_id) ?? ownerScope.parentRepoId ?? null : null;
  const resolvedScope =
    ownerScope.kind === 'worktree'
      ? { kind: 'worktree' as const, id: ownerScope.id, parentRepoId }
      : ownerScope;
  const redirectTo =
    resolvedScope.kind === 'worktree' && options.currentPath
      ? legacyWorktreeRedirectPath(options.currentPath, resolvedScope.id, parentRepoId)
      : null;

  const ownerKey = scopedOwnerKey(resolvedScope);
  const owner = ownerFromScope(resolvedScope);
  const tickets = snapshot.data.ticketQueue.map(mapTicketSummary);
  const runs = snapshot.data.runQueue.map(mapChatRunProgress);
  const chats = snapshot.data.chatQueue.map(mapChatSummary);
  hydrateScopedTicketQueue(store, ownerKey, owner, tickets, runs);

  const state = options.readModelState ?? store.snapshot();
  const ticketList = selectTicketSummaries(state, ownerKey);
  const chatRuns = selectChatRuns(state, ownerKey);
  const ticketDetail = mapTicketDetail(ticketRecord);
  const source = { tickets: ticketList, runs: chatRuns, chats, artifacts: [] as SurfaceArtifact[] };
  const detail = buildTicketDetailViewModel(ticketDetail, source);

  return {
    ok: true,
    ownerScope: resolvedScope,
    ownerKey,
    owner,
    ticketDetail,
    tickets: ticketList,
    runs: chatRuns,
    chats,
    dispatches: snapshot.data.dispatches ?? [],
    detail,
    currentRunId: detail.flowRunId,
    sectionIssues: [],
    parentRepoId,
    redirectTo
  };
}

export function ticketFlowEventShouldReload(payload: JsonRecord): boolean {
  const status = String(payload.status ?? payload.flow_status ?? payload.state ?? '').toLowerCase();
  const eventType = String(payload.event_type ?? payload.type ?? '').toLowerCase();
  return (
    ['completed', 'complete', 'done', 'failed', 'cancelled', 'canceled'].includes(status) ||
    eventType.includes('terminal')
  );
}

function hydrateScopedTicketQueue(
  store: ReadModelEntityStore,
  ownerKey: string,
  owner: ScopedTicketQueueOwner,
  tickets: TicketSummary[],
  runs: ChatRunProgress[]
): void {
  rememberTickets(owner, tickets);
  store.replaceScopedTicketSummaries(ownerKey, tickets);
  store.replaceScopedRuns(ownerKey, runs);
}

function renderTicketSummaryDetail(
  ticketList: TicketSummary[],
  ownerKey: string,
  routeTicketId: string,
  store: ReadModelEntityStore
): ScopedTicketCachedDetail | null {
  if (!ticketList.length) return null;
  const selected = resolveTicketRouteId(ticketList, routeTicketId);
  if (!selected) return null;
  store.replaceScopedTicketSummaries(ownerKey, ticketList);
  return {
    ownerKey,
    detail: buildTicketDetailViewModel(ticketDetailFromSummary(selected), {
      tickets: ticketList,
      runs: [],
      chats: [],
      artifacts: []
    })
  };
}

function ownerFromScope(ownerScope: Exclude<TicketOwnerScope, null>): ScopedTicketQueueOwner {
  return ownerScope.kind === 'repo' ? { repo: ownerScope.id } : { worktree: ownerScope.id };
}

function parentRepoIdFromSnapshot(snapshot: RepoWorktreeDetailSnapshot): string | null {
  return stringValue(snapshot.parentLinks.repo_id);
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function numberValue(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function recordValue(value: unknown): JsonRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : null;
}
