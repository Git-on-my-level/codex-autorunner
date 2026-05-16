import { browser } from '$app/environment';
import type { ApiError } from '$lib/api/client';
import {
  readModelEntityStore,
  type ReadModelEntityState,
  type ReadModelEntityStore
} from './readModelStore';
import {
  readModelSnapshotClient,
  type ChatIndexRequest,
  type ReadModelSnapshotClient
} from './readModelClients';
import type { TicketSummary } from '$lib/viewModels/domain';

export type ReadModelDependency = `${string}:${string}`;
export type ReadModelDepends = (...dependencies: ReadModelDependency[]) => void;

export type ReadModelLoaderOptions = {
  depends?: ReadModelDepends;
  refresh?: boolean;
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
  repoWorktreeIndex: 'entity:repo-worktree:index',
  chat: (chatId: string) => `entity:chat:${chatId}` as const,
  repo: (repoId: string) => `entity:repo:${repoId}` as const,
  worktree: (worktreeId: string) => `entity:worktree:${worktreeId}` as const,
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
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  if (!shouldRefresh(state, Boolean(state.chatIndexCursor), options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.chatIndex(request);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.applyChatIndexSnapshot(result.data, request);
  return { status: 'fetched', tags };
}

export async function ensureChatDetailLoaded(
  chatId: string,
  options: ReadModelLoaderOptions & { timelineLimit?: number } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.chat(chatId)];
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = Boolean(state.chatDetails[chatId]?.thread && state.timelines[chatId]);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.chatDetail(chatId, options.timelineLimit);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.applyChatDetailSnapshot(result.data);
  return { status: 'fetched', tags };
}

export async function ensureRepoWorktreeIndexLoaded(
  options: ReadModelLoaderOptions & { limit?: number } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.repoWorktreeIndex];
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = Boolean(state.cursors['repo_worktree.topology'] && state.cursors['repo_worktree.runtime']);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const [topology, runtime] = await Promise.all([
    client.repoWorktreeTopology('all', options.limit ?? 50),
    client.repoWorktreeRuntime('all', options.limit ?? 50)
  ]);
  if (!topology.ok) return { status: 'error', tags, error: topology.error };
  if (!runtime.ok) return { status: 'error', tags, error: runtime.error };
  store.applyRepoWorktreeTopologySnapshot(topology.data);
  store.applyRepoWorktreeRuntimeSnapshot(runtime.data);
  return { status: 'fetched', tags };
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
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = Boolean(state.tickets[ticketId] && state.cursors[`ticket.detail:${ticketId}`]);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.ticketDetail(ticketId, owner);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.applyTicketDetailSnapshot(result.data);
  return { status: 'fetched', tags };
}

export async function ensureRepoDetailLoaded(
  repoId: string,
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.repo(repoId)];
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = Boolean(state.repoDetails[repoId]);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.repoDetail(repoId);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.applyRepoDetailSnapshot(result.data);
  return { status: 'fetched', tags };
}

export async function ensureWorktreeDetailLoaded(
  worktreeId: string,
  options: ReadModelLoaderOptions = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.worktree(worktreeId)];
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const state = store.snapshot();
  const cached = Boolean(state.worktreeDetails[worktreeId]);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.worktreeDetail(worktreeId);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.applyWorktreeDetailSnapshot(result.data);
  return { status: 'fetched', tags };
}

export type TicketIndexOwner = { repo?: string; worktree?: string } | undefined;

export async function ensureTicketIndexLoaded(
  options: ReadModelLoaderOptions & { owner?: TicketIndexOwner } = {}
): Promise<ReadModelLoaderResult> {
  const tags = [readModelEntityTags.ticketIndex];
  markDepends(options.depends, tags);
  const store = options.store ?? readModelEntityStore;
  if (!browser) return { status: 'cold', tags };

  const ownerKey = options.owner
    ? options.owner.repo ? `repo:${options.owner.repo}` : options.owner.worktree ? `worktree:${options.owner.worktree}` : 'all'
    : 'all';
  const state = store.snapshot();
  const cached = Boolean(state.ticketOrderByOwner[ownerKey]);
  if (!shouldRefresh(state, cached, options)) return { status: 'cache-hit', tags };

  const client = options.client ?? readModelSnapshotClient;
  const result = await client.ticketIndex(options.owner);
  if (!result.ok) return { status: 'error', tags, error: result.error };
  store.replaceScopedTicketSummaries(ownerKey, result.data);
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
