import {
  type ApiResult,
  type JsonRecord,
  type RequestOptions
} from '$lib/api/client';
import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary } from '$lib/viewModels/domain';
import {
  buildTicketListViewModel,
  ticketRouteSegmentFromSummary,
  type SurfaceActionManifest,
  type TicketQueueAction,
  type TicketListViewModel,
  type TicketOwnerScope
} from '$lib/viewModels/ticket';
import type { ScopeRef } from '$lib/viewModels/scope';
import { formatScopeUrn, scopeLabel, scopeRoute, scopeTicketRoute } from '$lib/viewModels/scope';

export type ScopedTicketQueueKind = 'repo' | 'worktree';

export function scopeToTicketQueueConfig(scope: ScopeRef): ScopedTicketQueueConfig | null {
  if (scope.kind !== 'repo' && scope.kind !== 'worktree') return null;
  if (!scopeRoute(scope)) return null;
  return {
    kind: scope.kind,
    resourceId: scope.id,
    apiBasePath: `/repos/${encodeURIComponent(scope.id)}/api/flows`,
    displayLabel: scopeLabel(scope),
    ...(scope.kind === 'worktree' ? { parentRepoId: scope.parentRepoId ?? null } : {})
  };
}

export function scopeToTicketOwner(scope: ScopeRef): ScopedTicketQueueOwner | null {
  if (scope.kind === 'repo') return { repo: scope.id };
  if (scope.kind === 'worktree') return { worktree: scope.id };
  return null;
}

export function scopeToTicketOwnerScope(scope: ScopeRef): TicketOwnerScope {
  if (scope.kind === 'repo') return { kind: 'repo', id: scope.id };
  if (scope.kind === 'worktree') return { kind: 'worktree', id: scope.id, parentRepoId: scope.parentRepoId ?? null };
  return null;
}

export function ticketScopeUrn(scope: ScopeRef): string {
  return formatScopeUrn(scope);
}

export function ticketScopeHref(scope: ScopeRef): string | null {
  return scopeTicketRoute(scope);
}
export type ScopedTicketQueueCommand = 'start' | 'stop' | 'restart';
export type ScopedTicketQueueOwner = { repo: string } | { worktree: string };

export type ScopedTicketQueueConfig = {
  kind: ScopedTicketQueueKind;
  resourceId: string;
  apiBasePath: string;
  displayLabel: string;
  /** For worktree queues linked under a repo-owned URL (`/repos/.../worktrees/.../tickets`). */
  parentRepoId?: string | null;
};

export type ScopedTicketQueueCommandRequest = {
  path: string;
  options: RequestOptions;
};

export type ScopedTicketQueueCommandPlan = {
  requests: ScopedTicketQueueCommandRequest[];
};

export type ScopedTicketQueueCommandResult = {
  status: string | null;
  shouldReload: boolean;
};

type ScopedTicketQueueApi = {
  requestJson<T>(path: string, options?: RequestOptions): Promise<ApiResult<T>>;
  ticketFlow: {
    createTicket(
      body: { agent?: string; title?: string; goal?: string; body?: string; profile?: string },
      owner: ScopedTicketQueueOwner
    ): Promise<ApiResult<TicketDetail>>;
    reorderTicket(
      sourceIndex: number,
      destinationIndex: number,
      placeAfter: boolean,
      owner: ScopedTicketQueueOwner
    ): Promise<ApiResult<JsonRecord>>;
  };
};

export function scopedTicketQueueOwner(config: Pick<ScopedTicketQueueConfig, 'kind' | 'resourceId'>): ScopedTicketQueueOwner {
  return config.kind === 'repo' ? { repo: config.resourceId } : { worktree: config.resourceId };
}

export function scopedTicketQueueScope(config: ScopedTicketQueueConfig): Exclude<TicketOwnerScope, null> {
  if (config.kind === 'repo') return { kind: 'repo', id: config.resourceId };
  return { kind: 'worktree', id: config.resourceId, parentRepoId: config.parentRepoId ?? null };
}

export async function loadScopedActionManifest(
  api: Pick<ScopedTicketQueueApi, 'requestJson'>,
  config: ScopedTicketQueueConfig
): Promise<ApiResult<SurfaceActionManifest | null>> {
  const params = new URLSearchParams({
    ui_kind: 'pma_web',
    resource_kind: config.kind,
    resource_id: config.resourceId
  });
  return api.requestJson<SurfaceActionManifest>(`${config.apiBasePath}/ticket_flow/action-manifest?${params.toString()}`);
}

export async function createScopedTicket(
  api: ScopedTicketQueueApi,
  config: ScopedTicketQueueConfig,
  payload: { title: string; body: string }
): Promise<{ ok: boolean; status: string; ticketRouteId?: string | null }> {
  const result = await api.ticketFlow.createTicket(
    { agent: 'codex', title: payload.title, body: payload.body },
    scopedTicketQueueOwner(config)
  );
  if (!result.ok) return { ok: false, status: result.error.message };
  const ticketRouteId = ticketRouteSegmentFromSummary(result.data);
  return { ok: true, status: 'Ticket created.', ticketRouteId };
}

export async function reorderScopedTicket(
  api: ScopedTicketQueueApi,
  config: ScopedTicketQueueConfig,
  sourceRouteId: string,
  destinationRouteId: string,
  placeAfter: boolean
): Promise<{ ok: boolean; status: string }> {
  const sourceIndex = Number(sourceRouteId);
  const destinationIndex = Number(destinationRouteId);
  if (!Number.isInteger(sourceIndex) || !Number.isInteger(destinationIndex)) {
    return { ok: false, status: 'Only numbered tickets can be reordered.' };
  }
  const result = await api.ticketFlow.reorderTicket(
    sourceIndex,
    destinationIndex,
    placeAfter,
    scopedTicketQueueOwner(config)
  );
  return { ok: result.ok, status: result.ok ? 'Order saved.' : result.error.message };
}

export function scopedTicketActionStatus(
  action: 'create' | 'reorder' | ScopedTicketQueueCommand,
  config: Pick<ScopedTicketQueueConfig, 'displayLabel'>
): string {
  switch (action) {
    case 'create':
      return `Creating ${config.displayLabel} ticket...`;
    case 'reorder':
      return `Reordering ${config.displayLabel} tickets...`;
    case 'start':
      return `Starting ${config.displayLabel} ticket flow...`;
    case 'stop':
      return `Stopping ${config.displayLabel} ticket flow...`;
    case 'restart':
      return `Restarting ${config.displayLabel} ticket flow...`;
  }
}

export function scopedTicketMissingRunStatus(config: Pick<ScopedTicketQueueConfig, 'displayLabel'>): string {
  return `No ${config.displayLabel} ticket flow run found.`;
}

export function buildScopedTicketQueueCommandPlan(
  command: ScopedTicketQueueCommand,
  config: Pick<ScopedTicketQueueConfig, 'apiBasePath'>,
  runId: string | null
): ScopedTicketQueueCommandPlan | null {
  if ((command === 'stop' || command === 'restart') && !runId) return null;
  if (command === 'start') {
    return {
      requests: [{ path: `${config.apiBasePath}/ticket_flow/bootstrap`, options: { method: 'POST', body: {} } }]
    };
  }
  const stopRequest = {
    path: `${config.apiBasePath}/${encodeURIComponent(runId!)}/stop`,
    options: { method: 'POST' }
  };
  if (command === 'stop') return { requests: [stopRequest] };
  return {
    requests: [
      stopRequest,
      {
        path: `${config.apiBasePath}/ticket_flow/bootstrap`,
        options: { method: 'POST', body: { metadata: { force_new: true } } }
      }
    ]
  };
}

export async function runScopedTicketQueueCommand(
  api: Pick<ScopedTicketQueueApi, 'requestJson'>,
  config: ScopedTicketQueueConfig,
  command: ScopedTicketQueueCommand,
  runId: string | null,
  confirmRestart: () => boolean,
  action: TicketQueueAction | null = null
): Promise<ScopedTicketQueueCommandResult> {
  if (action?.requiresConfirmation && !confirmRestart()) {
    return { status: null, shouldReload: false };
  }
  if (action?.enabled && action.route) {
    const options: RequestOptions = { method: action.method };
    if (action.method === 'POST' && command === 'restart') {
      options.body = { metadata: { force_new: true } };
    }
    const result = await api.requestJson<JsonRecord>(action.route, options);
    return {
      status: result.ok ? 'Ticket flow command accepted.' : result.error.message,
      shouldReload: true
    };
  }
  if ((command === 'stop' || command === 'restart') && !runId) {
    return { status: scopedTicketMissingRunStatus(config), shouldReload: false };
  }
  if (command === 'restart' && !confirmRestart()) {
    return { status: null, shouldReload: false };
  }
  const plan = buildScopedTicketQueueCommandPlan(command, config, runId);
  if (!plan) return { status: scopedTicketMissingRunStatus(config), shouldReload: false };

  for (const request of plan.requests) {
    const result = await api.requestJson<JsonRecord>(request.path, request.options);
    if (!result.ok) return { status: result.error.message, shouldReload: true };
  }
  return { status: 'Ticket flow command accepted.', shouldReload: true };
}

export function buildScopedTicketList(
  tickets: TicketSummary[],
  runs: PmaRunProgress[],
  chats: PmaChatSummary[],
  scope: Exclude<TicketOwnerScope, null>,
  actionManifest: SurfaceActionManifest | null = null
): TicketListViewModel {
  return buildTicketListViewModel(
    {
      tickets,
      runs,
      chats,
      artifacts: [] as SurfaceArtifact[]
    },
    scope,
    actionManifest
  );
}
