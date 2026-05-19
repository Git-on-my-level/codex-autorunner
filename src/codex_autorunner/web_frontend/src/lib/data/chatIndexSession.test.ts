import { describe, expect, it, vi } from 'vitest';
import type { ApiResult } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatIndexPatchEvent,
  type ChatIndexRow,
  type ChatIndexSnapshot,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { ReadModelEntityStore, selectChatIndexWindowView } from './readModelStore';
import { selectPmaChats } from './readModelViewModels';
import { createChatIndexSession } from './chatIndexSession';
import type { ReadModelSnapshotClient } from './readModelClients';
import type { ChatIndexStreamFactory } from './chatIndexSession';
import type { ReadModelStreamManager, ReadModelStreamOptions } from './readModelStream';

const issuedAt = '2026-05-12T00:00:00.000Z';

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
    rows,
    groups: [],
    counters,
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
    expect(selectPmaChats(store.snapshot()).map((chat) => chat.id)).toEqual(['chat-active']);
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

    const rows = selectPmaChats(store.snapshot());
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
