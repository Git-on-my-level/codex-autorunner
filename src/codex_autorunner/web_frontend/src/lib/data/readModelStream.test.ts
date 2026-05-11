import { describe, expect, it, vi } from 'vitest';
import { ReadModelStreamManager, type CursorStorage } from './readModelStream';

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = [];
  closed = false;

  constructor(
    readonly url: string,
    readonly init?: EventSourceInit
  ) {
    super();
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: unknown, id = ''): void {
    const event = new MessageEvent(type, {
      data: typeof data === 'string' ? data : JSON.stringify(data),
      lastEventId: id
    });
    this.dispatchEvent(event);
  }

  fail(): void {
    this.dispatchEvent(new Event('error'));
  }
}

function memoryStorage(): CursorStorage {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key)
  };
}

describe('read model stream manager', () => {
  it('persists cursors from server-sent events', () => {
    FakeEventSource.instances = [];
    const storage = memoryStorage();
    const events: unknown[] = [];
    const statuses: string[] = [];
    const manager = new ReadModelStreamManager({
      key: 'chat.index',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch'],
      cursorStorage: storage,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      parse: (event) => event.data,
      onEvent: (event) => events.push(event),
      onStatus: (status) => statuses.push(status)
    });

    manager.open();
    FakeEventSource.instances[0].emit('chat.index.patch', { ok: true }, '42');

    expect(events).toEqual([{ ok: true }]);
    expect(storage.getItem('car.readModel.cursor.chat.index')).toBe('42');
    expect(statuses).toEqual(['connecting', 'connected']);
    manager.close();
  });

  it('resumes with persisted cursor and reconnects with backoff', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const storage = memoryStorage();
    storage.setItem('car.readModel.cursor.chat.index', '100');
    const manager = new ReadModelStreamManager({
      key: 'chat.index',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch'],
      cursorStorage: storage,
      reconnectBaseMs: 10,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      parse: (event) => event.data,
      onEvent: () => {}
    });

    manager.open();
    expect(FakeEventSource.instances[0].url).toContain('cursor=100');
    FakeEventSource.instances[0].fail();
    expect(FakeEventSource.instances[0].closed).toBe(true);
    vi.advanceTimersByTime(10);

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('cursor=100');
    manager.close();
    vi.useRealTimers();
  });
});
