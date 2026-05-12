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
