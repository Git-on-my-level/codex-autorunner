import { browser } from '$app/environment';
import type { ApiError } from '$lib/api/client';
import {
  canonicalChatIndexWindowKey,
  previewServicesWindowKey,
  readModelEntityStore,
  repoWorktreeWindowKey,
  type ReadModelEntityState,
  type ReadModelEntityStore
} from './readModelStore';
import {
  readModelSnapshotClient,
  type ChatIndexRequest,
  type ReadModelSnapshotClient
} from './readModelClients';

export type ReadModelDependency = `${string}:${string}`;
export type ReadModelDepends = (...dependencies: ReadModelDependency[]) => void;

export type ReadModelLoaderOptions = {
  depends?: ReadModelDepends;
  refresh?: boolean;
  blocking?: boolean;
  stale?: (state: ReadModelEntityState) => boolean;
  store?: ReadModelEntityStore;
  client?: ReadModelSnapshotClient;
};

export type TicketOwnerRef = {
  kind: 'repo' | 'worktree';
  id: string;
};

export type ReadModelLoaderResult =
  | { status: 'cache-hit'; tags: string[] }
  | { status: 'fetched'; tags: string[] }
  | { status: 'cold'; tags: string[] }
  | { status: 'error'; tags: string[]; error: ApiError };

export const readModelEntityTags = {
  chatIndex: 'entity:chat:index',
  automationWorkspace: 'entity:automation:workspace',
  repoWorktreeIndex: 'entity:repo-worktree:index',
  chat: (chatId: string) => `entity:chat:${chatId}` as const,
  repo: (repoId: string) => `entity:repo:${repoId}` as const,
  worktree: (worktreeId: string) => `entity:worktree:${worktreeId}` as const,
  serviceIndex: 'entity:service:index',
  ticket: (ticketId: string) => `entity:ticket:${ticketId}` as const,
  ticketIndex: 'entity:ticket:index' as const
} as const;

/**
 * Loader convention for issue #1758:
 * - universal +page/+layout loads call depends(tag) with these entity:* tags.
 * - ReadModelEntityStore is the durable browser cache; loaders return only status handles.
 * - loaders consult store.snapshot() first and fetch only on absence, stale policy, or refresh.
 * - browser-only fetch and store hydration is guarded because the app prerenders with ssr=false.
 * - mutation paths should call invalidate(readModelEntityTags.<entity>(id)) after successful writes.
 */
export async function ensureChatIndexLoaded(
  request: ChatIndexRequest = {},
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.chatIndex];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.chatWindows[canonicalChatIndexWindowKey(request)]),
    fetchAndApply: async (client, store) => {
      const result = await client.chatIndex(request);
      if (!result.ok) return result;
      store.applyChatIndexSnapshot(result.data, request);
      return result;
    }
  });
}

export async function ensureChatDetailLoaded(
  chatId: string,
  options: ReadModelLoaderOptions & { timelineLimit?: number } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.chat(chatId)];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.chatDetails[chatId]?.thread && state.timelines[chatId]),
    fetchAndApply: async (client, store) => {
      const result = await client.chatDetail(chatId, options.timelineLimit);
      if (!result.ok) return result;
      store.applyChatDetailSnapshot(result.data);
      return result;
    }
  });
}

export async function ensureAutomationWorkspaceLoaded(
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.automationWorkspace];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.automationWorkspace),
    fetchAndApply: async (client, store) => {
      const result = await client.automationWorkspaceIndex();
      if (!result.ok) return result;
      store.applyAutomationWorkspaceSnapshot(result.data);
      return result;
    }
  });
}

export async function ensureRepoWorktreeIndexLoaded(
  options: ReadModelLoaderOptions & { limit?: number } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.repoWorktreeIndex];
  const limit = options.limit ?? 200;
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => isRepoWorktreeIndexWindowCached(state, limit),
    fetchAndApply: async (client, store) => {
      const [topology, runtime] = await Promise.all([
        client.repoWorktreeTopology('all', limit),
        client.repoWorktreeRuntime('all', limit)
      ]);
      if (!topology.ok) return topology;
      if (!runtime.ok) return runtime;
      store.applyRepoWorktreeTopologySnapshot(topology.data);
      store.applyRepoWorktreeRuntimeSnapshot(runtime.data);
      return runtime;
    }
  });
}

export async function ensureServicesLoaded(
  options: ReadModelLoaderOptions & { scope?: string | null } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.serviceIndex];
  const scope = options.scope ?? null;
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.previewServices[previewServicesWindowKey(scope)]),
    fetchAndApply: async (client, store) => {
      const result = await client.servicesReadModel(scope);
      if (!result.ok) return result;
      store.applyServicesReadModelSnapshot(result.data, scope);
      return result;
    }
  });
}

export function isRepoWorktreeIndexWindowCached(
  state: ReadModelEntityState,
  limit = 200
): boolean {
  const window = state.repoWorktreeWindows[repoWorktreeWindowKey('all', limit)];
  if (!window?.topologyWindow || !window.runtimeWindow) return false;
  const topologyLoaded = window.repoIds.length + window.worktreeIds.length;
  const runtimeLoaded = window.runtimeIds.length;
  return (
    windowCompletesRequest(window.topologyWindow, topologyLoaded, limit) &&
    windowCompletesRequest(window.runtimeWindow, runtimeLoaded, limit)
  );
}

function windowCompletesRequest(
  window: { limit: number; totalEstimate?: number | null; totalIsExact: boolean },
  loaded: number,
  requestedLimit: number
): boolean {
  if (window.limit < requestedLimit) return false;
  if (!window.totalIsExact || window.totalEstimate == null) return loaded >= requestedLimit;
  return loaded >= Math.min(window.totalEstimate, requestedLimit);
}

export async function ensureTicketDetailLoaded(
  ticketId: string,
  owner: TicketOwnerRef,
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [
    readModelEntityTags.ticket(ticketId),
    owner.kind === 'repo' ? readModelEntityTags.repo(owner.id) : readModelEntityTags.worktree(owner.id)
  ];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.tickets[ticketId] && state.cursors[`ticket.detail:${ticketId}`]),
    fetchAndApply: async (client, store) => {
      const result = await client.ticketDetail(ticketId, owner);
      if (!result.ok) return result;
      store.applyTicketDetailSnapshot(result.data);
      return result;
    }
  });
}

export async function ensureRepoDetailLoaded(
  repoId: string,
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.repo(repoId)];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.repoDetails[repoId]),
    fetchAndApply: async (client, store) => {
      const result = await client.repoDetail(repoId);
      if (!result.ok) return result;
      store.applyRepoDetailSnapshot(result.data);
      return result;
    }
  });
}

export async function ensureWorktreeDetailLoaded(
  worktreeId: string,
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.worktree(worktreeId)];
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.worktreeDetails[worktreeId]),
    fetchAndApply: async (client, store) => {
      const result = await client.worktreeDetail(worktreeId);
      if (!result.ok) return result;
      store.applyWorktreeDetailSnapshot(result.data);
      return result;
    }
  });
}

export type TicketIndexOwner = { repo?: string; worktree?: string } | undefined;

export async function ensureTicketIndexLoaded(
  options: ReadModelLoaderOptions & { owner?: TicketIndexOwner } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.ticketIndex];
  const ownerKey = options.owner
    ? options.owner.repo ? `repo:${options.owner.repo}` : options.owner.worktree ? `worktree:${options.owner.worktree}` : 'all'
    : 'all';
  return ensureSnapshotLoaded({
    tags,
    options,
    isCached: (state) => Boolean(state.ticketOrderByOwner[ownerKey]),
    fetchAndApply: async (client, store) => {
      const result = await client.ticketIndex(options.owner);
      if (!result.ok) return result;
      store.replaceScopedTicketSummaries(ownerKey, result.data);
      return result;
    }
  });
}

type SnapshotLifecycleOptions = {
  tags: ReadModelDependency[];
  options: ReadModelLoaderOptions;
  isCached: (state: ReadModelEntityState) => boolean;
  fetchAndApply: (
    client: ReadModelSnapshotClient,
    store: ReadModelEntityStore
  ) => Promise<{ ok: true } | { ok: false; error: ApiError }>;
};

async function ensureSnapshotLoaded({
  tags,
  options,
  isCached,
  fetchAndApply
}: SnapshotLifecycleOptions): Promise<ReadModelLoaderResult> {
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = isCached(state);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  if (options.blocking === false) {
    if (cached) {
      void fetchAndApply(client, store).catch(() => undefined);
      return { status: 'cache-hit', tags };
    }
    return { status: 'cold', tags };
  }

  const result = await fetchAndApply(client, store);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  return { status: 'fetched', tags };
}

function shouldRefresh(
  state: ReadModelEntityState,
  cached: boolean,
  options: ReadModelLoaderOptions
): boolean {
  return options.refresh === true || !cached || options.stale?.(state) === true;
}

function markDepends(depends: ReadModelDepends | undefined, tags: ReadModelDependency[]): void {
  for (const tag of tags) depends?.(tag);
}
