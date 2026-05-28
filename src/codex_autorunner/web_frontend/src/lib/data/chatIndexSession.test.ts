import { describe, expect, it, vi } from 'vitest';
import type { ApiResult } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatIndexPatchEvent,
  type ChatIndexRow,
  type ChatIndexSnapshot,
  type ProjectionCursor,
  type TicketRunGroup
} from '$lib/api/readModelContracts';
import type { StreamVisibilityPolicy } from '$lib/runtime/streamVisibilityPolicy';
import { ReadModelEntityStore, selectChatIndexWindowView } from './readModelStore';
import { selectChats } from './readModelViewModels';
import { createChatIndexSession } from './chatIndexSession';
import type { ReadModelSnapshotClient } from './readModelClients';
import type { ChatIndexStreamFactory } from './chatIndexSession';
import type { ReadModelStreamManager, ReadModelStreamOptions } from './readModelStream';

const issuedAt = '2026-05-12T00:00:00.000Z';
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
const ticketRunFacetRequest = { facets: { categories: ['ticket_run' as const] }, groupBy: 'ticket_run' as const, limit: 50 };

function projCursor(sequence: number, source: string): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt };
}

function chatIndexSnapshot(
  filter: ChatIndexSnapshot['filter'],
  rows: ChatIndexRow[]
): ChatIndexSnapshot {
  const counters = {
    total: rows.length,
    waiting: rows.filter((r) => r.status === 'waiting').length,
    running: rows.filter((r) => r.status === 'running').length,
    unread: rows.filter((r) => r.unreadCount > 0).length,
    archived: rows.filter((r) => r.status === 'archived').length
  };
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.index.snapshot',
    cursor: projCursor(1, 'test.chat.index'),
    window: {
      limit: 200,
      nextCursor: null,
      previousCursor: null,
      totalEstimate: rows.length,
      totalIsExact: true
    },
    filter,
    query: null,
    facetRequest: emptyFacetRequest,
    rows,
    groups: [],
    counters,
    facetCounts: emptyFacetCounts,
    repair: {
      snapshotRoute: '/hub/read-models/chats',
      cursorQueryParam: 'after',
      gapEventType: 'projection.cursor_gap',
      behavior: 'repair_snapshot_required'
    }
  };
}

function indexRow(
  id: string,
  title: string,
  status: ChatIndexRow['status'],
  lastActivityAt: string = issuedAt
): ChatIndexRow {
  return {
    chatId: id,
    surface: 'pma',
    title,
    status,
    unreadCount: 0,
    lastActivityAt,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    runId: null,
    agent: null,
    agentProfile: null,
    chatKind: null,
    model: null,
    groupId: null
  };
}

function ticketFlowRow(
  id: string,
  status: ChatIndexRow['status'],
  lastActivityAt: string = issuedAt
): ChatIndexRow {
  return {
    ...indexRow(id, id, status, lastActivityAt),
    worktreeId: 'wt-1',
    ticketId: id,
    runId: 'run-1',
    flowType: 'ticket_flow',
    groupId: 'ticket-run:run-1',
    facets: {
      category: 'ticket_run',
      turnKinds: ['message'],
      originKinds: ['surface'],
      transports: ['pma'],
      scopeKind: 'worktree',
      scopeId: 'wt-1',
      agentKind: 'coding_agent'
    },
    ticketDone: status === 'idle',
    ticketStatus: status === 'idle' ? 'done' : status === 'running' ? 'running' : 'unknown'
  };
}

function ticketRunGroup(overrides: Partial<TicketRunGroup> = {}): TicketRunGroup {
  return {
    kind: 'ticket_run_group',
    groupId: 'ticket-run:run-1',
    runId: 'run-1',
    scopeKind: 'worktree',
    scopeId: 'wt-1',
    label: 'Ticket run run-1',
    status: 'running',
    totalCount: 5,
    doneCount: 3,
    runningCount: 2,
    waitingCount: 0,
    failedCount: 0,
    unreadCount: 0,
    updatedAt: issuedAt,
    ...overrides
  };
}

describe('chat index session', () => {
  it('starts once and refreshes the chat index from the canonical snapshot client', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const streamFactory = mockStreamFactory();
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    expect(streamFactory).not.toHaveBeenCalled();
    await session.refresh();

    const firstPage = store.subscribe(() => {});
    firstPage();
    const secondPage = store.subscribe(() => {});
    secondPage();
    session.start();

    expect(client.chatIndex).toHaveBeenCalledTimes(1);
    expect(client.chatIndex).toHaveBeenNthCalledWith(1, { filter: 'all', limit: 50 });
    expect(streamFactory).toHaveBeenCalledWith(expect.objectContaining({
      key: 'chat.index.entity',
      path: '/hub/read-models/chats/patches?filter=all',
      eventTypes: ['chat.index.patch', 'projection.cursor_gap']
    }));
    const streamOptions = streamFactory.mock.calls[0]?.[0] as ReadModelStreamOptions<ChatIndexPatchEvent>;
    expect(streamOptions.cursorStorage?.getItem('car.readModel.cursor.chat.index.entity')).toBe('1');
    expect(selectChats(store.snapshot()).map((chat) => chat.id)).toEqual(['chat-active']);
    expect(session.isStarted()).toBe(true);

    session.stop();
    expect(session.isStarted()).toBe(false);
  });

  it('has no production surface-event writer that can replace chat order', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const session = createChatIndexSession({ client, store, streamFactory: mockStreamFactory() });

    session.start();
    await session.refresh();
    const replaceSpy = vi.spyOn(store, 'replaceChatIndexRows');
    const applySpy = vi.spyOn(store, 'applyChatIndexSnapshot');

    session.start();

    const rows = selectChats(store.snapshot());
    expect(rows.map((chat) => chat.id)).toEqual(['chat-active']);
    expect(replaceSpy).not.toHaveBeenCalled();
    expect(applySpy).not.toHaveBeenCalled();
  });

  it('manual refreshes are deterministic for identical backend snapshots', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const session = createChatIndexSession({ client, store });

    await session.refresh();
    const first = store.snapshot().chatOrder.map((id) => store.snapshot().chats[id]?.title);

    await session.refresh();
    const second = store.snapshot().chatOrder.map((id) => store.snapshot().chats[id]?.title);

    expect(second).toEqual(first);
    expect(store.snapshot().chatOrder).toEqual(['chat-active']);
  });

  it('loads the next backend window into the same filtered search result', async () => {
    const store = new ReadModelEntityStore();
    const client = {
      chatIndex: vi.fn(async (request = {}) => {
        const pageTwo = request.cursor === '2';
        return ok({
          ...chatIndexSnapshot('archived', [
            pageTwo
              ? indexRow('chat-archived-2', 'Needle archived page 2', 'archived')
              : indexRow('chat-archived-1', 'Needle archived page 1', 'archived')
          ]),
          query: 'needle',
          window: {
            limit: 1,
            nextCursor: pageTwo ? null : '2',
            previousCursor: pageTwo ? '0' : null,
            totalEstimate: 2,
            totalIsExact: true
          },
          counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 2 }
        });
      })
    } as unknown as ReadModelSnapshotClient & { chatIndex: ReturnType<typeof vi.fn> };
    const session = createChatIndexSession({ client, store, streamFactory: mockStreamFactory() });
    const request = { filter: 'archived' as const, query: 'needle', limit: 1 };

    await session.refresh(request);
    await session.loadMore(request);

    expect(client.chatIndex).toHaveBeenNthCalledWith(1, request);
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, { ...request, cursor: '2' });
    expect(selectChatIndexWindowView(store.snapshot(), request).rows.map((row) => row.chatId)).toEqual([
      'chat-archived-1',
      'chat-archived-2'
    ]);
    expect(selectChatIndexWindowView(store.snapshot(), request).window?.window?.nextCursor).toBeNull();
  });

  it('queues a follow-up snapshot when refresh parameters change during an in-flight request', async () => {
    const store = new ReadModelEntityStore();
    const firstResponse = deferred<ApiResult<ChatIndexSnapshot>>();
    const client = {
      chatIndex: vi.fn((request = {}) => {
        if (request.filter === 'archived') {
          return Promise.resolve(ok(chatIndexSnapshot('archived', [indexRow('chat-archived', 'Archived chat', 'archived')])));
        }
        return firstResponse.promise;
      })
    } as unknown as ReadModelSnapshotClient;
    const session = createChatIndexSession({ client, store, streamFactory: mockStreamFactory() });

    const refresh = session.refresh({ filter: 'all', limit: 200 });
    const queuedRefresh = session.refresh({ filter: 'archived', limit: 200 });
    firstResponse.resolve(ok(chatIndexSnapshot('all', [indexRow('chat-active', 'Active chat', 'running')])));
    await queuedRefresh;
    await refresh;

    expect(client.chatIndex).toHaveBeenCalledTimes(2);
    expect(client.chatIndex).toHaveBeenNthCalledWith(1, { filter: 'all', limit: 200 });
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, { filter: 'archived', limit: 200 });
    expect(store.snapshot().chatOrder).toEqual(['chat-active', 'chat-archived']);
    expect(Object.keys(store.snapshot().chats).sort()).toEqual(['chat-active', 'chat-archived']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-active']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'archived', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-archived']);
  });

  it('keeps the chat-index entity stream stable across filter refreshes', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const streamFactory = mockStreamFactory();
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    await session.refresh({ filter: 'archived', limit: 200 });

    expect(streamFactory).toHaveBeenCalledTimes(1);
    expect(streamFactory).toHaveBeenNthCalledWith(1, expect.objectContaining({
      key: 'chat.index.entity',
      path: '/hub/read-models/chats/patches?filter=all'
    }));
  });

  it('opts the chat-index entity stream into visibility suspension and repairs on resume', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const visibilityPolicy = fakeVisibilityPolicy();
    const createdStreamOptions: ReadModelStreamOptions<ChatIndexPatchEvent>[] = [];
    const streamFactory = vi.fn((options: ReadModelStreamOptions<ChatIndexPatchEvent>) => {
      createdStreamOptions.push(options);
      return {
        open: vi.fn(),
        close: vi.fn(),
        cursor: vi.fn(() => null),
        resetCursor: vi.fn()
      } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>;
    }) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
    const session = createChatIndexSession({ client, store, streamFactory, visibilityPolicy });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    const streamOptions = createdStreamOptions[0];
    expect(streamOptions?.visibilityPolicy).toBe(visibilityPolicy);

    await streamOptions?.onResume?.();

    expect(client.chatIndex).toHaveBeenCalledTimes(2);
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, { filter: 'all', limit: 50 });
  });

  it('replaces the chat-index entity stream exactly once when activation changes its canonical request', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const streams: ReadModelStreamManager<ChatIndexPatchEvent>[] = [];
    const streamFactory = vi.fn((options: ReadModelStreamOptions<ChatIndexPatchEvent>) => {
      const manager = {
        open: vi.fn(),
        close: vi.fn(),
        cursor: vi.fn(() => null),
        resetCursor: vi.fn(),
        options
      } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>;
      streams.push(manager);
      return manager;
    }) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    await session.activate({ primaryRequest: { filter: 'archived', limit: 50 } });

    expect(streamFactory).toHaveBeenCalledTimes(2);
    expect(streamFactory).toHaveBeenNthCalledWith(1, expect.objectContaining({
      path: '/hub/read-models/chats/patches?filter=all'
    }));
    expect(streamFactory).toHaveBeenNthCalledWith(2, expect.objectContaining({
      path: '/hub/read-models/chats/patches?filter=archived'
    }));
    expect(streams[0]?.close).toHaveBeenCalledTimes(1);
    expect(client.chatIndex).toHaveBeenLastCalledWith({ filter: 'archived', limit: 50 });
  });

  it('does not churn stream or refresh snapshots for a no-op activation', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const streamFactory = mockStreamFactory();
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    await session.activate({ primaryRequest: { filter: 'all', limit: 50 } });

    expect(streamFactory).toHaveBeenCalledTimes(1);
    expect(client.chatIndex).toHaveBeenCalledTimes(1);
  });

  it('refreshes changed companion requests without replacing the active stream', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const streamFactory = mockStreamFactory();
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    await session.activate({
      companionRequests: [ticketRunFacetRequest]
    });

    expect(streamFactory).toHaveBeenCalledTimes(1);
    expect(client.chatIndex).toHaveBeenCalledTimes(3);
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, { filter: 'all', limit: 50 });
    expect(client.chatIndex).toHaveBeenNthCalledWith(3, ticketRunFacetRequest);
  });

  it('refreshes companion ticket-run aggregate windows with the primary chat index', async () => {
    const store = new ReadModelEntityStore();
    const client = {
      chatIndex: vi.fn(async (request = {}) => {
        if (request.facets?.categories?.includes('ticket_run')) {
          return ok({
            ...chatIndexSnapshot('all', [ticketFlowRow('ticket-1', 'running')]),
            facetRequest: { ...emptyFacetRequest, categories: ['ticket_run'] },
            groups: [ticketRunGroup({ doneCount: 3, runningCount: 2 })],
            counters: { total: 5, waiting: 0, running: 2, unread: 0, archived: 0 }
          });
        }
        return ok(chatIndexSnapshot('all', [indexRow('chat-active', 'Active chat', 'running')]));
      })
    } as unknown as ReadModelSnapshotClient;
    const session = createChatIndexSession({ client, store, streamFactory: mockStreamFactory() });

    session.setCompanionRequests([ticketRunFacetRequest]);
    await session.refresh({ filter: 'all', limit: 50 });

    expect(client.chatIndex).toHaveBeenCalledTimes(2);
    expect(client.chatIndex).toHaveBeenNthCalledWith(1, { filter: 'all', limit: 50 });
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, ticketRunFacetRequest);
    expect(selectChatIndexWindowView(store.snapshot(), ticketRunFacetRequest).groups[0]).toMatchObject({
      kind: 'ticket_run_group',
      groupId: 'ticket-run:run-1',
      doneCount: 3,
      runningCount: 2
    });
  });

  it('repairs stale companion ticket-run aggregates when ticket-flow rows change', async () => {
    const store = new ReadModelEntityStore();
    let streamOptions: ReadModelStreamOptions<ChatIndexPatchEvent> | null = null;
    const streamFactory = vi.fn((options: ReadModelStreamOptions<ChatIndexPatchEvent>) => {
      streamOptions = options;
      return {
        open: vi.fn(),
        close: vi.fn(),
        cursor: vi.fn(() => null),
        resetCursor: vi.fn()
      } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>;
    }) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
    let ticketRunRefreshes = 0;
    const client = {
      chatIndex: vi.fn(async (request = {}) => {
        if (request.facets?.categories?.includes('ticket_run')) {
          ticketRunRefreshes += 1;
          const refreshed = ticketRunRefreshes > 1;
          return ok({
            ...chatIndexSnapshot('all', [ticketFlowRow('ticket-1', refreshed ? 'idle' : 'running')]),
            facetRequest: { ...emptyFacetRequest, categories: ['ticket_run'] },
            groups: [ticketRunGroup(refreshed ? { doneCount: 4, runningCount: 1 } : { doneCount: 3, runningCount: 2 })],
            counters: { total: 5, waiting: 0, running: refreshed ? 1 : 2, unread: 0, archived: 0 }
          });
        }
        return ok({
          ...chatIndexSnapshot('waiting', [
            indexRow('chat-visible', 'Visible chat', 'idle'),
            indexRow('chat-window', 'Window chat', 'idle')
          ]),
          window: {
            limit: 2,
            nextCursor: null,
            previousCursor: null,
            totalEstimate: 2,
            totalIsExact: true
          },
          counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 0 }
        });
      })
    } as unknown as ReadModelSnapshotClient & { chatIndex: ReturnType<typeof vi.fn> };
    const session = createChatIndexSession({ client, store, streamFactory });

    session.setCompanionRequests([ticketRunFacetRequest]);
    await session.refresh({ filter: 'waiting', limit: 2 });
    session.start();
    const options = streamOptions as unknown as ReadModelStreamOptions<ChatIndexPatchEvent>;
    options.onEvent?.({
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.index.patch',
        cursor: projCursor(2, 'test.chat.index'),
        entityKind: 'chat',
        entityId: 'ticket-1',
        operation: 'patch',
        generatedAt: issuedAt
      },
      patch: {
        rows: [ticketFlowRow('ticket-1', 'idle')],
        groups: [],
        removedRowIds: [],
        removedGroupIds: [],
        counters: { total: 3, waiting: 0, running: 0, unread: 0, archived: 0 }
      }
    }, null);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(client.chatIndex.mock.calls.filter(([request]) => request?.facets?.categories?.includes('ticket_run'))).toHaveLength(2);
    expect(selectChatIndexWindowView(store.snapshot(), ticketRunFacetRequest).groups[0]).toMatchObject({
      doneCount: 4,
      runningCount: 1
    });
  });

  it('refreshes the current window when stream patches leave it under-filled', async () => {
    const store = new ReadModelEntityStore();
    let streamOptions: ReadModelStreamOptions<ChatIndexPatchEvent> | null = null;
    const streamFactory = vi.fn((options: ReadModelStreamOptions<ChatIndexPatchEvent>) => {
      streamOptions = options;
      return {
        open: vi.fn(),
        close: vi.fn(),
        cursor: vi.fn(() => null),
        resetCursor: vi.fn()
      } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>;
    }) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
    const client = {
      chatIndex: vi
        .fn()
        .mockResolvedValueOnce(ok({
          ...chatIndexSnapshot('all', [
            indexRow('chat-visible', 'Visible chat', 'idle'),
            indexRow('chat-window', 'Window chat', 'idle')
          ]),
          window: {
            limit: 2,
            nextCursor: null,
            previousCursor: null,
            totalEstimate: 3,
            totalIsExact: true
          },
          counters: { total: 3, waiting: 0, running: 0, unread: 0, archived: 0 }
        }))
        .mockResolvedValue(ok({
          ...chatIndexSnapshot('all', [
            indexRow('chat-window', 'Window chat', 'idle'),
            indexRow('chat-backfill', 'Backfill chat', 'idle')
          ]),
          window: {
            limit: 2,
            nextCursor: null,
            previousCursor: null,
            totalEstimate: 2,
            totalIsExact: true
          },
          counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 1 }
        }))
    } as unknown as ReadModelSnapshotClient;
    const session = createChatIndexSession({ client, store, streamFactory });

    await session.activate({ primaryRequest: { filter: 'all', limit: 2 }, refresh: false });
    session.start();
    await session.refresh({ filter: 'all', limit: 2 });
    expect(streamOptions).not.toBeNull();
    const options = streamOptions as unknown as ReadModelStreamOptions<ChatIndexPatchEvent>;
    options.onEvent?.({
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.index.patch',
        cursor: projCursor(2, 'test.chat.index'),
        entityKind: 'chat',
        entityId: 'chat-visible',
        operation: 'patch',
        generatedAt: issuedAt
      },
      patch: {
        rows: [],
        groups: [],
        removedRowIds: ['chat-visible'],
        removedGroupIds: [],
        counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 1 }
      }
    }, null);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(client.chatIndex).toHaveBeenCalledTimes(2);
    expect(client.chatIndex).toHaveBeenNthCalledWith(2, { filter: 'all', limit: 2 });
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 2 }).rows.map((row) => row.chatId)).toEqual([
      'chat-window',
      'chat-backfill'
    ]);
  });

  it('repairs invalidated windows with one snapshot refresh', async () => {
    const store = new ReadModelEntityStore();
    let streamOptions: ReadModelStreamOptions<ChatIndexPatchEvent> | null = null;
    const streamFactory = vi.fn((options: ReadModelStreamOptions<ChatIndexPatchEvent>) => {
      streamOptions = options;
      return {
        open: vi.fn(),
        close: vi.fn(),
        cursor: vi.fn(() => null),
        resetCursor: vi.fn()
      } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>;
    }) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
    const client = mockClient();
    const session = createChatIndexSession({ client, store, streamFactory });

    session.start();
    await session.refresh({ filter: 'all', limit: 50 });
    const options = streamOptions as unknown as ReadModelStreamOptions<ChatIndexPatchEvent>;
    options.onEvent?.({
      ...chatPatchEvent(2, indexRow('chat-archived-history', 'Archived history', 'archived')),
      envelope: {
        ...chatPatchEvent(2, indexRow('chat-archived-history', 'Archived history', 'archived')).envelope,
        eventType: 'projection.cursor_gap',
        operation: 'invalidate'
      },
      patch: {
        rows: [],
        groups: [],
        removedRowIds: [],
        removedGroupIds: [],
        counters: { total: 1, waiting: 0, running: 1, unread: 0, archived: 4000 }
      }
    }, null);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(client.chatIndex).toHaveBeenCalledTimes(2);
    expect(Object.keys(store.snapshot().chats)).toEqual(['chat-active']);
  });
});

function mockClient(): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn(async (request = {}) => {
      if (request.filter === 'archived') {
        return ok(chatIndexSnapshot('archived', [indexRow('chat-archived', 'Archived chat', 'archived')]));
      }
      return ok(chatIndexSnapshot('all', [indexRow('chat-active', 'Active chat', 'running')]));
    })
  } as unknown as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function mockStreamFactory(): ReturnType<typeof vi.fn> & ChatIndexStreamFactory {
  return vi.fn((_options: ReadModelStreamOptions<ChatIndexPatchEvent>) => ({
    open: vi.fn(),
    close: vi.fn(),
    cursor: vi.fn(() => null),
    resetCursor: vi.fn()
  } as unknown as ReadModelStreamManager<ChatIndexPatchEvent>)) as ReturnType<typeof vi.fn> & ChatIndexStreamFactory;
}

function fakeVisibilityPolicy(): StreamVisibilityPolicy {
  return {
    suspendWhenHidden: true,
    isVisible: () => true,
    subscribe: () => () => {}
  };
}

function chatPatchEvent(sequence: number, row: ChatIndexRow): ChatIndexPatchEvent {
  return {
    envelope: {
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      eventType: 'chat.index.patch',
      cursor: projCursor(sequence, 'test.chat.index'),
      entityKind: 'chat',
      entityId: row.chatId,
      operation: 'patch',
      generatedAt: issuedAt
    },
    patch: {
      rows: [row],
      groups: [],
      removedRowIds: [],
      removedGroupIds: [],
      counters: {
        total: 1,
        waiting: row.status === 'waiting' ? 1 : 0,
        running: row.status === 'running' ? 1 : 0,
        unread: 0,
        archived: row.status === 'archived' ? 1 : 0
      }
    }
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}
