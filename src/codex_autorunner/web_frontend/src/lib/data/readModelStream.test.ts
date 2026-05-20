import { describe, expect, it, vi } from 'vitest';
import type { StreamVisibilityPolicy } from '$lib/runtime/streamVisibilityPolicy';
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

class FakeVisibilityPolicy implements StreamVisibilityPolicy {
  suspendWhenHidden = true;
  private visibleValue = true;
  private listeners = new Set<(visible: boolean) => void>();

  isVisible(): boolean {
    return this.visibleValue;
  }

  subscribe(listener: (visible: boolean) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  setVisible(visible: boolean): void {
    this.visibleValue = visible;
    this.listeners.forEach((listener) => listener(visible));
  }

  listenerCount(): number {
    return this.listeners.size;
  }
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

  it('cancels pending reconnects while hidden and resumes from the cursor when visible', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const storage = memoryStorage();
    const visibilityPolicy = new FakeVisibilityPolicy();
    const onResume = vi.fn();
    const manager = new ReadModelStreamManager({
      key: 'chat.index',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch'],
      cursorStorage: storage,
      reconnectBaseMs: 10,
      visibilityPolicy,
      onResume,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      parse: (event) => event.data,
      onEvent: () => {}
    });

    manager.open();
    FakeEventSource.instances[0].emit('chat.index.patch', { ok: true }, '42');
    FakeEventSource.instances[0].fail();
    visibilityPolicy.setVisible(false);
    vi.advanceTimersByTime(10);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].closed).toBe(true);

    visibilityPolicy.setVisible(true);

    expect(onResume).toHaveBeenCalledOnce();
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('cursor=42');
    manager.close();
    vi.useRealTimers();
  });

  it('does not open hidden streams until visibility returns', () => {
    FakeEventSource.instances = [];
    const visibilityPolicy = new FakeVisibilityPolicy();
    visibilityPolicy.setVisible(false);
    const statuses: string[] = [];
    const manager = new ReadModelStreamManager({
      key: 'chat.index',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch'],
      visibilityPolicy,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      parse: (event) => event.data,
      onEvent: () => {},
      onStatus: (status) => statuses.push(status)
    });

    manager.open();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(statuses).toEqual(['interrupted']);

    visibilityPolicy.setVisible(true);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(statuses).toEqual(['interrupted', 'connecting']);
    manager.close();
  });

  it('unsubscribes visibility listeners and closes idempotently', () => {
    FakeEventSource.instances = [];
    const visibilityPolicy = new FakeVisibilityPolicy();
    const statuses: string[] = [];
    const manager = new ReadModelStreamManager({
      key: 'chat.index',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch'],
      visibilityPolicy,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      parse: (event) => event.data,
      onEvent: () => {},
      onStatus: (status) => statuses.push(status)
    });

    manager.open();
    expect(visibilityPolicy.listenerCount()).toBe(1);

    manager.close();
    manager.close();
    visibilityPolicy.setVisible(true);

    expect(visibilityPolicy.listenerCount()).toBe(0);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(statuses.filter((status) => status === 'closed')).toHaveLength(1);
  });
});
