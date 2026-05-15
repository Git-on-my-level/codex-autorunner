import { describe, expect, it, vi } from 'vitest';
import type { ApiResult, JsonRecord, PmaApiClient } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatIndexRow,
  type ChatIndexSnapshot,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import type { ChatSurfaceStreamOptions, StreamSubscription } from '$lib/api/streaming';
import { ReadModelEntityStore } from './readModelStore';
import { selectPmaChats } from './readModelViewModels';
import { createChatIndexSession } from './chatIndexSession';

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
  it('keeps one chat index stream while chat pages mount and unmount inside a layout session', async () => {
    const store = new ReadModelEntityStore();
    const close = vi.fn();
    const openStream = vi.fn((_options: ChatSurfaceStreamOptions): StreamSubscription => ({ close }));
    const api = mockApi();
    const session = createChatIndexSession({ api, store, openStream });

    session.start();
    await session.refresh();

    const firstPage = store.subscribe(() => {});
    firstPage();
    const secondPage = store.subscribe(() => {});
    secondPage();
    session.start();

    expect(openStream).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
    expect(selectPmaChats(store.snapshot()).map((chat) => chat.id)).toEqual(['chat-active', 'chat-archived']);

    session.stop();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('keeps live stream updates from reordering existing chat rows under the cursor', async () => {
    const store = new ReadModelEntityStore();
    const streamOptions: ChatSurfaceStreamOptions[] = [];
    const openStream = vi.fn((options: ChatSurfaceStreamOptions): StreamSubscription => {
      streamOptions.push(options);
      return { close: vi.fn() };
    });
    const session = createChatIndexSession({ api: mockApi(), store, openStream });

    session.start();
    await session.refresh();

    const options = streamOptions[0];
    if (!options) throw new Error('stream was not opened');
    options.onEvent({
      kind: 'chat_snapshot',
      lastEventId: 'evt-1',
      payload: {
        surfaces: [
          chatSurface('chat-new', 'New chat'),
          chatSurface('chat-active', 'Active chat renamed'),
          chatSurface('chat-archived', 'Archived chat')
        ]
      }
    });

    const rows = selectPmaChats(store.snapshot());
    expect(rows.map((chat) => chat.id)).toEqual(['chat-active', 'chat-archived', 'chat-new']);
    expect(rows[0].title).toBe('Active chat renamed');
  });

  it('ignores incremental chat events already covered by the latest snapshot cursor', async () => {
    const store = new ReadModelEntityStore();
    const streamOptions: ChatSurfaceStreamOptions[] = [];
    const openStream = vi.fn((options: ChatSurfaceStreamOptions): StreamSubscription => {
      streamOptions.push(options);
      return { close: vi.fn() };
    });
    const session = createChatIndexSession({ api: mockApi(), store, openStream });

    session.start();
    await session.refresh();

    const options = streamOptions[0];
    if (!options) throw new Error('stream was not opened');
    options.onEvent({
      kind: 'chat_snapshot',
      lastEventId: '12',
      payload: {
        cursor: 12,
        surfaces: [chatSurface('chat-active', 'Snapshot title')]
      }
    });
    options.onEvent({
      kind: 'chat_event',
      lastEventId: '11',
      payload: {
        cursor: 11,
        event_type: 'queue.state_changed',
        surface: { surface_kind: 'pma', surface_key: 'chat-active' },
        managed_thread_id: 'chat-active',
        lifecycle: 'queued',
        lifecycle_status: 'active',
        status: 'queued',
        occurred_at: '2026-05-13T00:00:00Z'
      }
    });

    const rows = selectPmaChats(store.snapshot());
    expect(rows[0]).toMatchObject({
      id: 'chat-active',
      title: 'Snapshot title',
      status: 'idle',
      updatedAt: '2026-05-12T00:00:00Z'
    });
  });

  it('lets metadata-only chat events update titles without changing activity time', async () => {
    const store = new ReadModelEntityStore();
    const streamOptions: ChatSurfaceStreamOptions[] = [];
    const openStream = vi.fn((options: ChatSurfaceStreamOptions): StreamSubscription => {
      streamOptions.push(options);
      return { close: vi.fn() };
    });
    const session = createChatIndexSession({ api: mockApi(), store, openStream });

    session.start();
    await session.refresh();

    const options = streamOptions[0];
    if (!options) throw new Error('stream was not opened');
    options.onEvent({
      kind: 'chat_event',
      lastEventId: '13',
      payload: {
        cursor: 13,
        event_type: 'channel_directory.discovered',
        surface: { surface_kind: 'pma', surface_key: 'chat-active' },
        managed_thread_id: 'chat-active',
        lifecycle: 'discovered',
        lifecycle_status: 'active',
        status: 'discovered',
        occurred_at: '2026-05-13T00:00:00Z',
        details: { channel: { display: 'Agent Nexus / #codex' } }
      }
    });

    const rows = selectPmaChats(store.snapshot());
    expect(rows[0]).toMatchObject({
      id: 'chat-active',
      title: 'Agent Nexus / #codex',
      updatedAt: '2026-05-12T00:00:00.000Z'
    });
  });
});

function mockApi(): PmaApiClient {
  return {
    getJson: vi.fn(async (path: string): Promise<ApiResult<JsonRecord>> => {
      if (path.includes('filter=archived')) {
        return ok(
          chatIndexSnapshot('archived', [
            indexRow('chat-archived', 'Archived chat', 'archived')
          ]) as unknown as JsonRecord
        );
      }
      if (path.includes('filter=all')) {
        return ok(
          chatIndexSnapshot('all', [indexRow('chat-active', 'Active chat', 'running')]) as unknown as JsonRecord
        );
      }
      return ok({ rows: [] });
    })
  } as unknown as PmaApiClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function chatSurface(id: string, title: string): JsonRecord {
  return {
    surface_kind: 'pma',
    surface_key: id,
    managed_thread_id: id,
    facts: ['managed_thread'],
    lifecycle_status: 'active',
    resource_owner: {},
    display: { display_name: title },
    metadata: { runtime_status: 'idle' },
    updated_at: '2026-05-12T00:00:00Z'
  };
}
