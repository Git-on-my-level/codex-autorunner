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
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapTicketDetail,
  mapTicketSummary,
  type PmaChatSummary,
  type PmaRunProgress,
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
  type TicketDetailViewModel,
  type TicketOwnerScope
} from '$lib/viewModels/ticket';
import { cachedTickets, rememberTickets } from '$lib/viewModels/ticketCache';
import { readModelEntityStore, type ReadModelEntityState, type ReadModelEntityStore } from './readModelStore';
import { scopedOwnerKey, selectPmaRuns, selectTicketSummaries } from './readModelViewModels';

export type ScopedTicketSessionApi = {
  requestJson<T>(path: string, options?: unknown): Promise<ApiResult<T>>;
  readModels: {
    repoDetail(repoId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
    worktreeDetail(worktreeId: string): Promise<ApiResult<RepoWorktreeDetailSnapshot>>;
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
      runs: PmaRunProgress[];
      actionManifest: SurfaceActionManifest | null;
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
      runs: PmaRunProgress[];
      chats: PmaChatSummary[];
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
  const detail =
    config.kind === 'repo'
      ? await api.readModels.repoDetail(config.resourceId)
      : await api.readModels.worktreeDetail(config.resourceId);
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
  const runs = detail.data.runQueue.map(mapPmaRunProgress);
  hydrateScopedTicketQueue(options.store ?? readModelEntityStore, ownerKey, owner, tickets, runs);

  const manifest = await loadScopedActionManifest(api, { ...config, parentRepoId: config.parentRepoId ?? parentRepoId });
  const actionManifest = dataOr(manifest, null);
  const sectionIssues = [
    !manifest.ok ? partialPageIssue('action_manifest', 'Action manifest unavailable', manifest.error) : null
  ].filter((issue): issue is PartialPageIssue => Boolean(issue));

  return {
    ok: true,
    owner,
    ownerScope,
    ownerKey,
    tickets,
    runs,
    actionManifest,
    sectionIssues,
    parentRepoId,
    redirectTo
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
  const runs = snapshot.data.runQueue.map(mapPmaRunProgress);
  const chats = snapshot.data.chatQueue.map(mapPmaChatSummary);
  hydrateScopedTicketQueue(store, ownerKey, owner, tickets, runs);

  const state = options.readModelState ?? store.snapshot();
  const ticketList = selectTicketSummaries(state, ownerKey);
  const pmaRuns = selectPmaRuns(state, ownerKey);
  const ticketDetail = mapTicketDetail(ticketRecord);
  const source = { tickets: ticketList, runs: pmaRuns, chats, artifacts: [] as SurfaceArtifact[] };
  const detail = buildTicketDetailViewModel(ticketDetail, source);

  return {
    ok: true,
    ownerScope: resolvedScope,
    ownerKey,
    owner,
    ticketDetail,
    tickets: ticketList,
    runs: pmaRuns,
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
  runs: PmaRunProgress[]
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
