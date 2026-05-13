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
import { ReadModelEntityStore, selectChatDetailView } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';

const now = '2026-05-11T12:00:00Z';

describe('/chats route load', () => {
  it('loads the chat index and returns list-mode data when no active chat id is present', async () => {
    const depends = vi.fn();
    const client = mockClient();
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ depends, loaderOptions: { client } });

    expect(result).toEqual({
      chatId: null,
      chatIndex: { status: 'fetched', tags: ['entity:chat:index'] },
      activeDetail: null
    });
    expect(depends).toHaveBeenCalledWith('entity:chat:index');
    expect(client.chatIndex).toHaveBeenCalledTimes(1);
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
      chatIndex: { status: 'fetched', tags: ['entity:chat:index'] },
      activeDetail: { status: 'cache-hit', tags: ['entity:chat:chat-1'] }
    });
    expect(result.activeDetail?.status).not.toBe('cold');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.title).toBe('Chat detail');
    expect(client.chatDetail).not.toHaveBeenCalled();
  });

  it('fetches and hydrates a missing active chat detail', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(ok(chatDetailSnapshot('chat-2')))
    });
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ chatId: 'chat-2', loaderOptions: { store, client } });

    expect(result.chatIndex.status).toBe('fetched');
    expect(result.activeDetail?.status).toBe('fetched');
    expect(client.chatDetail).toHaveBeenCalledWith('chat-2', undefined);
    expect(selectChatDetailView(store.snapshot(), 'chat-2').thread?.title).toBe('Chat detail');
    expect(store.snapshot().pmaTimelines['chat-2']?.order).toEqual(['item-1']);
  });

  it('returns an error handle when active chat detail fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot unavailable');
    const client = mockClient({
      chatDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { loadChatRoute } = await importPageLoad(true);

    const result = await loadChatRoute({ chatId: 'chat-1', loaderOptions: { store, client } });

    expect(result).toEqual({
      chatId: 'chat-1',
      chatIndex: { status: 'fetched', tags: ['entity:chat:index'] },
      activeDetail: { status: 'error', tags: ['entity:chat:chat-1'], error }
    });
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread).toBeNull();
  });
});

async function importPageLoad(browser: boolean) {
  vi.resetModules();
  vi.doMock('$app/environment', () => ({ browser, dev: false, building: false, version: 'test' }));
  return import('./[[chatId]]/loadChatRoute');
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

function chatIndexSnapshot(): ChatIndexSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.index.snapshot',
    cursor: cursor(1, 'chat.index'),
    window: { limit: 50, totalEstimate: 1, totalIsExact: true },
    filter: 'all',
    query: null,
    rows: [],
    groups: [],
    counters: { total: 0, waiting: 0, running: 0, unread: 0, archived: 0 },
    repair: repair('/hub/chat/index')
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
      artifactIds: []
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
