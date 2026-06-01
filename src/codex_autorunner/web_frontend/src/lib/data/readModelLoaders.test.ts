import { describe, expect, it, vi } from 'vitest';
import { READ_MODEL_CONTRACT_VERSION, type ChatDetailSnapshot, type ChatIndexSnapshot, type ProjectionCursor, type RepoWorktreeRuntimeSnapshot, type RepoWorktreeTopologySnapshot, type TicketDetailSnapshot } from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { ReadModelEntityStore, selectChatDetailView } from './readModelStore';
import type { ReadModelSnapshotClient } from './readModelClients';
import type { TicketSummary } from '$lib/viewModels/domain';

const now = '2026-05-11T12:00:00Z';
const emptyFacetRequest = {
  categories: [],
  turnKinds: [],
  originKinds: [],
  transports: [],
  scopeKinds: [],
  scopeIds: [],
  agentKinds: []
};
const emptyFacetCounts = {
  category: {},
  turnKind: {},
  originKind: {},
  transport: {},
  scopeKind: {},
  agentKind: {}
};

describe('read model loaders', () => {
  it('returns a cache hit and calls depends when the chat index is already in the store', async () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot(chatIndexSnapshot());
    const client = mockClient();
    const depends = vi.fn();
    const { ensureChatIndexLoaded, readModelEntityTags } = await importLoaders(true);

    const result = await ensureChatIndexLoaded({}, { store, client, depends });

    expect(result).toEqual({ status: 'cache-hit', tags: [readModelEntityTags.chatIndex] });
    expect(depends).toHaveBeenCalledWith('entity:chat:index');
    expect(client.chatIndex).not.toHaveBeenCalled();
  });

  it('returns cached data immediately and refreshes in the background when requested non-blocking', async () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot(chatIndexSnapshot());
    const client = mockClient({
      chatIndex: vi.fn().mockResolvedValue(ok(chatIndexSnapshot([chatIndexRow('chat-2', 'Fresh Chat')])))
    });
    const { ensureChatIndexLoaded } = await importLoaders(true);

    const result = await ensureChatIndexLoaded({}, { store, client, blocking: false, refresh: true });

    expect(result).toEqual({ status: 'cache-hit', tags: ['entity:chat:index'] });
    expect(client.chatIndex).toHaveBeenCalledWith({});
    await Promise.resolve();
    await Promise.resolve();
    expect(store.snapshot().chats['chat-2']?.title).toBe('Fresh Chat');
  });

  it('fetches a cache miss through the snapshot client and hydrates the store', async () => {
    const store = new ReadModelEntityStore();
    const snapshot = chatDetailSnapshot('chat-1');
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(ok(snapshot))
    });
    const { ensureChatDetailLoaded } = await importLoaders(true);

    const result = await ensureChatDetailLoaded('chat-1', { store, client, timelineLimit: 25 });

    expect(result.status).toBe('fetched');
    expect(client.chatDetail).toHaveBeenCalledWith('chat-1', 25);
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.title).toBe('Chat detail');
  });

  it('can return a cold placeholder on a cache miss without blocking route navigation', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(ok(chatDetailSnapshot('chat-1')))
    });
    const { ensureChatDetailLoaded } = await importLoaders(true);

    const result = await ensureChatDetailLoaded('chat-1', { store, client, blocking: false });

    expect(result).toEqual({ status: 'cold', tags: ['entity:chat:chat-1'] });
    expect(client.chatDetail).not.toHaveBeenCalled();
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread).toBeNull();
  });

  it('returns an error result without mutating the store when fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot unavailable');
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { ensureChatDetailLoaded } = await importLoaders(true);

    const result = await ensureChatDetailLoaded('chat-1', { store, client });

    expect(result).toEqual({ status: 'error', tags: ['entity:chat:chat-1'], error });
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread).toBeNull();
  });

  it('returns a cold placeholder and does not fetch when browser is false', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { ensureChatIndexLoaded } = await importLoaders(false);

    const result = await ensureChatIndexLoaded({}, { store, client, depends });

    expect(result).toEqual({ status: 'cold', tags: ['entity:chat:index'] });
    expect(depends).toHaveBeenCalledWith('entity:chat:index');
    expect(client.chatIndex).not.toHaveBeenCalled();
  });

  it('hydrates repo-worktree index snapshots from existing snapshot clients', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot())),
      repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot()))
    });
    const { ensureRepoWorktreeIndexLoaded } = await importLoaders(true);

    const result = await ensureRepoWorktreeIndexLoaded({ store, client });

    expect(result.status).toBe('fetched');
    expect(client.repoWorktreeTopology).toHaveBeenCalledWith('all', 200);
    expect(client.repoWorktreeRuntime).toHaveBeenCalledWith('all', 200);
    expect(store.snapshot().repos['repo-1']?.label).toBe('Repo One');
    expect(store.snapshot().runtime['repo:repo-1']?.activeRunStatus).toBe('running');
  });

  it('can defer repo-worktree index snapshots for non-blocking page loads', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot())),
      repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot()))
    });
    const { ensureRepoWorktreeIndexLoaded } = await importLoaders(true);

    const result = await ensureRepoWorktreeIndexLoaded({ store, client, blocking: false });

    expect(result).toEqual({ status: 'cold', tags: ['entity:repo-worktree:index'] });
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
    expect(client.repoWorktreeRuntime).not.toHaveBeenCalled();
  });

  it('allows repo-worktree index callers to request a smaller profile window explicitly', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot())),
      repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot()))
    });
    const { ensureRepoWorktreeIndexLoaded } = await importLoaders(true);

    const result = await ensureRepoWorktreeIndexLoaded({ store, client, limit: 50 });

    expect(result.status).toBe('fetched');
    expect(client.repoWorktreeTopology).toHaveBeenCalledWith('all', 50);
    expect(client.repoWorktreeRuntime).toHaveBeenCalledWith('all', 50);
  });

  it('does not treat a smaller support window as the full repo-worktree index', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoWorktreeTopologySnapshot(repoWorktreeTopologySnapshot({ limit: 50, totalEstimate: 67 }));
    store.applyRepoWorktreeRuntimeSnapshot(repoWorktreeRuntimeSnapshot({ limit: 50, totalEstimate: 67 }));
    const client = mockClient({
      repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot({ limit: 200, totalEstimate: 67 }))),
      repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot({ limit: 200, totalEstimate: 67 })))
    });
    const { ensureRepoWorktreeIndexLoaded } = await importLoaders(true);

    const result = await ensureRepoWorktreeIndexLoaded({ store, client, blocking: false });

    expect(result).toEqual({ status: 'cold', tags: ['entity:repo-worktree:index'] });
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
    expect(client.repoWorktreeRuntime).not.toHaveBeenCalled();
  });

  it('returns a cache hit for a complete repo-worktree index window', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoWorktreeTopologySnapshot(repoWorktreeTopologySnapshot());
    store.applyRepoWorktreeRuntimeSnapshot(repoWorktreeRuntimeSnapshot());
    const client = mockClient();
    const { ensureRepoWorktreeIndexLoaded } = await importLoaders(true);

    const result = await ensureRepoWorktreeIndexLoaded({ store, client, blocking: false });

    expect(result).toEqual({ status: 'cache-hit', tags: ['entity:repo-worktree:index'] });
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
    expect(client.repoWorktreeRuntime).not.toHaveBeenCalled();
  });

  it('uses ticket and owner entity tags for scoped ticket details', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const client = mockClient({
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot()))
    });
    const { ensureTicketDetailLoaded } = await importLoaders(true);

    const result = await ensureTicketDetailLoaded('ticket-1', { kind: 'repo', id: 'repo-1' }, { store, client, depends });

    expect(result).toEqual({ status: 'fetched', tags: ['entity:ticket:ticket-1', 'entity:repo:repo-1'] });
    expect(depends).toHaveBeenCalledWith('entity:ticket:ticket-1');
    expect(depends).toHaveBeenCalledWith('entity:repo:repo-1');
    expect(store.snapshot().tickets['ticket-1']?.title).toBe('Ticket One');
  });

  it('fetches and stores ticket index summaries in the store', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const summaries = [ticketSummary('t-1', 'Ticket A'), ticketSummary('t-2', 'Ticket B')];
    const client = mockClient({
      ticketIndex: vi.fn().mockResolvedValue(ok(summaries))
    });
    const { ensureTicketIndexLoaded } = await importLoaders(true);

    const result = await ensureTicketIndexLoaded({ store, client, depends });

    expect(result).toEqual({ status: 'fetched', tags: ['entity:ticket:index'] });
    expect(depends).toHaveBeenCalledWith('entity:ticket:index');
    expect(store.snapshot().ticketOrderByOwner['all']).toEqual(['t-1', 't-2']);
    expect(store.snapshot().ticketSummaries['t-1']?.title).toBe('Ticket A');
  });

  it('returns cache hit when ticket index is already in the store', async () => {
    const store = new ReadModelEntityStore();
    store.replaceScopedTicketSummaries('all', [ticketSummary('t-1', 'Cached')]);
    const client = mockClient();
    const depends = vi.fn();
    const { ensureTicketIndexLoaded } = await importLoaders(true);

    const result = await ensureTicketIndexLoaded({ store, client, depends });

    expect(result).toEqual({ status: 'cache-hit', tags: ['entity:ticket:index'] });
    expect(client.ticketIndex).not.toHaveBeenCalled();
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { ensureTicketIndexLoaded } = await importLoaders(false);

    const result = await ensureTicketIndexLoaded({ store, client, depends });

    expect(result).toEqual({ status: 'cold', tags: ['entity:ticket:index'] });
    expect(client.ticketIndex).not.toHaveBeenCalled();
  });
});

async function importLoaders(browser: boolean) {
  vi.resetModules();
  vi.doMock('$app/environment', () => ({ browser, dev: false, building: false, version: 'test' }));
  return import('./readModelLoaders');
}

function mockClient(overrides: Partial<Record<keyof ReadModelSnapshotClient, ReturnType<typeof vi.fn>>> = {}): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn().mockResolvedValue(ok(chatIndexSnapshot())),
    chatDetail: vi.fn().mockResolvedValue(ok(chatDetailSnapshot())),
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot())),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot())),
    repoDetail: vi.fn(),
    worktreeDetail: vi.fn(),
    ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot())),
    ticketIndex: vi.fn().mockResolvedValue(ok([])),
    ...overrides
  } as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: ApiError): ApiResult<T> {
  return { ok: false, error };
}

function apiError(message: string): ApiError {
  return { kind: 'http', status: 503, code: 'unavailable', message };
}

function cursor(sequence: number, source = 'test'): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt: now };
}

function chatIndexSnapshot(rows: ChatIndexSnapshot['rows'] = [chatIndexRow('chat-1', 'Chat One')]): ChatIndexSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.index.snapshot',
    cursor: cursor(1, 'chat.index'),
    window: { limit: 50, totalEstimate: 1, totalIsExact: true },
    filter: 'all',
    query: null,
    facetRequest: emptyFacetRequest,
    rows,
    groups: [],
    counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 },
    facetCounts: emptyFacetCounts,
    repair: repair('/hub/read-models/chats')
  };
}

function chatIndexRow(chatId: string, title: string): ChatIndexSnapshot['rows'][number] {
  return {
    chatId,
    surface: 'pma',
    title,
    status: 'idle',
    unreadCount: 0,
    lastActivityAt: now,
    repoId: 'repo-1',
    worktreeId: null,
    ticketId: null,
    runId: null,
    agent: 'codex',
    chatKind: 'pma',
    model: 'gpt-5.5',
    groupId: null
  };
}

function chatDetailSnapshot(chatId = 'chat-1'): ChatDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.detail.snapshot',
    cursor: cursor(2, 'chat.detail'),
    thread: {
      chatId,
      surface: 'pma',
      title: 'Chat detail',
      status: 'running',
      repoId: 'repo-1',
      worktreeId: null,
      ticketId: null,
      runId: 'run-1',
      agent: 'codex',
      chatKind: 'coding_agent',
      model: 'gpt-5.5',
      archived: false
    },
    timelineWindow: { limit: 50, totalEstimate: 1, totalIsExact: true },
    timeline: [{
      itemId: 'item-1',
      kind: 'user_message',
      role: 'user',
      createdAt: now,
      text: 'hello',
      artifactIds: [],
      identity: {
        timelineItemId: 'item-1',
        progressItemIds: [],
        correlationId: null
      },
      provenance: {
        sourceEventIds: [],
        progressEventIds: [],
        cursorEventId: null
      }
    }],
    queue: { depth: 0, queuedTurnIds: [] },
    artifacts: [],
    repair: repair(`/hub/read-models/chats/${chatId}`)
  };
}

function repoWorktreeTopologySnapshot(options: { limit?: number; totalEstimate?: number } = {}): RepoWorktreeTopologySnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.topology.snapshot',
    cursor: cursor(3, 'repo_worktree.topology'),
    window: { limit: options.limit ?? 200, totalEstimate: options.totalEstimate ?? 2, totalIsExact: true },
    repos: [{ repoId: 'repo-1', label: 'Repo One', path: '/repo', archived: false, childWorktreeIds: ['worktree-1'] }],
    worktrees: [{ worktreeId: 'worktree-1', repoId: 'repo-1', label: 'Worktree One', path: '/repo/wt', branch: 'main', archived: false }],
    repair: repair('/hub/read-models/repo-worktree/topology')
  };
}

function repoWorktreeRuntimeSnapshot(options: { limit?: number; totalEstimate?: number } = {}): RepoWorktreeRuntimeSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.runtime.snapshot',
    cursor: cursor(4, 'repo_worktree.runtime'),
    window: { limit: options.limit ?? 200, totalEstimate: options.totalEstimate ?? 1, totalIsExact: true },
    runtime: [{
      entityKind: 'repo',
      entityId: 'repo-1',
      activeRunStatus: 'running',
      waitingTicketCount: 1,
      runningTicketCount: 1,
      chatCount: 2,
      cleanupBlockers: []
    }],
    repair: repair('/hub/read-models/repo-worktree/runtime')
  };
}

function ticketDetailSnapshot(): TicketDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'ticket.detail.snapshot',
    cursor: cursor(5, 'ticket.detail'),
    ticket: {
      ticketId: 'ticket-1',
      routeId: 'TICKET-001',
      title: 'Ticket One',
      status: 'running',
      ownerKind: 'repo',
      ownerId: 'repo-1',
      agent: 'codex',
      model: 'gpt-5.5',
      done: false,
      updatedAt: now
    },
    siblings: [],
    linkedRun: null,
    linkedChats: [],
    artifacts: [],
    dispatchWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    dispatches: [],
    ticketDetail: {},
    ticketQueue: [],
    runQueue: [],
    chatQueue: [],
    repair: repair('/hub/read-models/tickets/ticket-1')
  };
}

function repair(snapshotRoute: string) {
  return {
    snapshotRoute,
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}

function ticketSummary(id: string, title: string): TicketSummary {
  return {
    id,
    number: null,
    title,
    status: 'waiting',
    workspaceKind: 'unscoped',
    workspaceId: null,
    repoId: null,
    worktreeId: null,
    agentId: null,
    path: null,
    ticketPath: null,
    workspacePath: null,
    errors: [],
    diffStats: null,
    durationSeconds: null,
    chatKey: null,
    runId: null,
    updatedAt: null,
    raw: {}
  };
}
