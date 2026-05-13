import { describe, expect, it, vi } from 'vitest';
import type { ApiResult, JsonRecord, PmaApiClient } from '$lib/api/client';
import type { ChatSurfaceStreamOptions, StreamSubscription } from '$lib/api/streaming';
import { ReadModelEntityStore } from './readModelStore';
import { selectPmaChats } from './readModelViewModels';
import { createChatIndexSession } from './chatIndexSession';

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
});

function mockApi(): PmaApiClient {
  return {
    getJson: vi.fn(async (path: string): Promise<ApiResult<JsonRecord>> => {
      if (path.includes('view=archived')) {
        return ok({ rows: [chatRow('chat-archived', 'Archived chat', 'archived')] });
      }
      return ok({ rows: [chatRow('chat-active', 'Active chat', 'running')] });
    })
  } as unknown as PmaApiClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function chatRow(id: string, title: string, status: string): JsonRecord {
  return {
    managed_thread_id: id,
    title,
    lifecycle_status: status,
    runtime_status: status,
    updated_at: '2026-05-12T00:00:00Z',
    surface: 'pma'
  };
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
