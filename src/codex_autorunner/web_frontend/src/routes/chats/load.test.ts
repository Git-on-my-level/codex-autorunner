import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatDetailSnapshot,
  type ChatIndexSnapshot,
  type ProjectionCursor,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST, ReadModelEntityStore, selectChatDetailView } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';
import { importRouteLoader } from '$lib/test/importRouteLoader';

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

describe('/chats route load', () => {
  it('returns cold list-mode data without blocking navigation when no active chat id is present', async () => {
    const depends = vi.fn();
    const client = mockClient();
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ depends, loaderOptions: { client } });

    expect(result).toEqual({
      chatId: null,
      chatIndex: { status: 'cold', tags: ['entity:chat:index'] },
      activeDetail: null
    });
    expect(depends).toHaveBeenCalledWith('entity:chat:index');
    expect(client.chatIndex).not.toHaveBeenCalled();
  });

  it('registers the chat index and active chat entity dependencies', async () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(chatDetailSnapshot('chat-1'));
    const depends = vi.fn();
    const { loadChatRoute } = await importPageLoad(true);

    await loadChatRoute({ chatId: 'chat-1', depends, loaderOptions: { store, client: mockClient() } });

    expect(depends).toHaveBeenCalledWith('entity:chat:index');
    expect(depends).toHaveBeenCalledWith('entity:chat:chat-1');
  });

  it('returns a cache hit for an already hydrated active chat', async () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(chatDetailSnapshot('chat-1'));
    const client = mockClient();
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ chatId: 'chat-1', loaderOptions: { store, client } });

    expect(result).toEqual({
      chatId: 'chat-1',
      chatIndex: { status: 'cold', tags: ['entity:chat:index'] },
      activeDetail: { status: 'cache-hit', tags: ['entity:chat:chat-1'] }
    });
    expect(result.activeDetail?.status).not.toBe('cold');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.title).toBe('Chat detail');
    expect(client.chatDetail).not.toHaveBeenCalled();
  });

  it('fetches a fresh deterministic chat index on repeated route loads', async () => {
    const store = new ReadModelEntityStore();
    const rows = [
      chatIndexRow('chat-b', 'Chat B', '2026-05-11T12:01:00Z'),
      chatIndexRow('chat-a', 'Chat A', '2026-05-11T12:00:00Z')
    ];
    const client = mockClient({
      chatIndex: vi.fn().mockResolvedValue(ok(chatIndexSnapshot(rows)))
    });
    const { loadChatRoute } = await importPageLoad(true);

    await loadChatRoute({ loaderOptions: { store, client, blocking: true } });
    const firstOrder = [...store.snapshot().chatOrder];
    const firstTitles = firstOrder.map((id) => store.snapshot().chats[id]?.title);
    await loadChatRoute({ loaderOptions: { store, client, blocking: true } });

    expect(client.chatIndex).toHaveBeenCalledTimes(4);
    expect(client.chatIndex).toHaveBeenNthCalledWith(1, { limit: 50 });
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST);
    expect(store.snapshot().chatOrder).toEqual(firstOrder);
    expect(store.snapshot().chatOrder).toEqual(['chat-b', 'chat-a']);
    expect(store.snapshot().chatOrder.map((id) => store.snapshot().chats[id]?.title)).toEqual(firstTitles);
  });

  it('fetches and hydrates a missing active chat detail', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(ok(chatDetailSnapshot('chat-2')))
    });
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ chatId: 'chat-2', loaderOptions: { store, client, blocking: true } });

    expect(result.chatIndex.status).toBe('fetched');
    expect(result.activeDetail?.status).toBe('fetched');
    expect(client.chatDetail).toHaveBeenCalledWith('chat-2', 50);
    expect(selectChatDetailView(store.snapshot(), 'chat-2').thread?.title).toBe('Chat detail');
  });

  it('returns an error handle when active chat detail fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot unavailable');
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ chatId: 'chat-1', loaderOptions: { store, client, blocking: true } });

    expect(result).toEqual({
      chatId: 'chat-1',
      chatIndex: { status: 'fetched', tags: ['entity:chat:index'] },
      activeDetail: { status: 'error', tags: ['entity:chat:chat-1'], error }
    });
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread).toBeNull();
  });
});

async function importPageLoad(browser: boolean) {
  return importRouteLoader<typeof import('$lib/routes/loadChatRoute')>('$lib/routes/loadChatRoute', browser);
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

function chatIndexSnapshot(rows: ChatIndexSnapshot['rows'] = []): ChatIndexSnapshot {
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
    counters: {
      total: rows.length,
      waiting: rows.filter((row) => row.status === 'waiting').length,
      running: rows.filter((row) => row.status === 'running').length,
      unread: rows.filter((row) => row.unreadCount > 0).length,
      archived: rows.filter((row) => row.status === 'archived').length
    },
    facetCounts: emptyFacetCounts,
    repair: repair('/hub/read-models/chats')
  };
}

function chatIndexRow(chatId: string, title: string, lastActivityAt: string): ChatIndexSnapshot['rows'][number] {
  return {
    chatId,
    surface: 'pma',
    title,
    status: 'idle',
    unreadCount: 0,
    lastActivityAt,
    repoId: null,
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

function repoWorktreeTopologySnapshot(): RepoWorktreeTopologySnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.topology.snapshot',
    cursor: cursor(3, 'repo_worktree.topology'),
    window: { limit: 200, totalEstimate: 0, totalIsExact: true },
    repos: [],
    worktrees: [],
    repair: repair('/hub/read-models/repo-worktree/topology')
  };
}

function repoWorktreeRuntimeSnapshot(): RepoWorktreeRuntimeSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.runtime.snapshot',
    cursor: cursor(4, 'repo_worktree.runtime'),
    window: { limit: 200, totalEstimate: 0, totalIsExact: true },
    runtime: [],
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
