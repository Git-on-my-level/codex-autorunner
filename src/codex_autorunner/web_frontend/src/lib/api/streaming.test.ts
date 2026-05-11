import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  normalizeChatSurfaceStreamEvent,
  normalizePmaChatStreamEvent,
  normalizePmaTailStreamEvent,
  openFlowRunEventSource,
  openChatSurfaceEventSource,
  openPmaChatEventSource,
  openPmaTailEventSource,
  parseJsonSseFrame,
  parseSseFrame
} from './streaming';

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
    this.dispatchEvent(
      new MessageEvent(type, {
        data: typeof data === 'string' ? data : JSON.stringify(data),
        lastEventId: id
      })
    );
  }

  fail(): void {
    this.dispatchEvent(new Event('error'));
  }
}

describe('SSE helpers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    if (typeof localStorage !== 'undefined') localStorage.clear();
    FakeEventSource.instances = [];
  });

  it('parses named SSE frames with ids and retry hints', () => {
    const parsed = parseSseFrame('id: 42\nevent: tail\nretry: 5000\ndata: {"summary":"Edited"}\n\n');

    expect(parsed).toEqual({
      id: '42',
      event: 'tail',
      retry: 5000,
      data: '{"summary":"Edited"}'
    });
  });

  it('parses JSON data and normalizes PMA tail events', () => {
    const parsed = parseJsonSseFrame('id: 9\nevent: progress\ndata: {"phase":"testing"}\n\n');
    expect(parsed).not.toBeNull();
    const normalized = normalizePmaTailStreamEvent(parsed!);

    expect(normalized).toEqual({
      kind: 'progress',
      lastEventId: '9',
      payload: { phase: 'testing' }
    });
  });

  it('normalizes PMA timeline stream events', () => {
    const parsed = parseJsonSseFrame('id: 7\nevent: timeline\ndata: {"item_id":"turn:1:intermediate:0001","kind":"intermediate"}\n\n');
    expect(parsed).not.toBeNull();

    expect(normalizePmaTailStreamEvent(parsed!)).toEqual({
      kind: 'timeline',
      lastEventId: '7',
      payload: { item_id: 'turn:1:intermediate:0001', kind: 'intermediate' }
    });
  });

  it('normalizes PMA chat snapshot stream events', () => {
    const parsed = parseJsonSseFrame('id: abc\nevent: chat_snapshot\ndata: {"threads":[{"managed_thread_id":"thread-1"}]}\n\n');
    expect(parsed).not.toBeNull();

    expect(normalizePmaChatStreamEvent(parsed!)).toEqual({
      kind: 'chat_snapshot',
      lastEventId: 'abc',
      payload: { threads: [{ managed_thread_id: 'thread-1' }] }
    });
  });

  it('normalizes generic chat surface stream events', () => {
    const snapshot = parseJsonSseFrame('id: 12\nevent: chat.snapshot\ndata: {"surfaces":[{"surface_kind":"discord"}]}\n\n');
    const event = parseJsonSseFrame('id: 13\nevent: chat.event\ndata: {"event_type":"surface.bound"}\n\n');

    expect(normalizeChatSurfaceStreamEvent(snapshot!)).toEqual({
      kind: 'chat_snapshot',
      lastEventId: '12',
      payload: { surfaces: [{ surface_kind: 'discord' }] }
    });
    expect(normalizeChatSurfaceStreamEvent(event!)).toEqual({
      kind: 'chat_event',
      lastEventId: '13',
      payload: { event_type: 'surface.bound' }
    });
  });

  it('opens PMA tail EventSource under the configured hub base path', () => {
    const close = vi.fn();
    const addEventListener = vi.fn();
    const eventSource = vi.fn(function EventSourceMock() {
      return { addEventListener, close };
    });
    vi.stubGlobal('EventSource', eventSource);

    const subscription = openPmaTailEventSource('thread/1', { onEvent: vi.fn() }, '/car');

    expect(eventSource).toHaveBeenCalledWith('/car/hub/pma/threads/thread%2F1/tail/events', {
      withCredentials: undefined
    });
    expect(addEventListener).toHaveBeenCalledWith('timeline', expect.any(Function));
    subscription.close();
    expect(close).toHaveBeenCalledOnce();
  });

  it('opens PMA chat EventSource under the configured hub base path', () => {
    const close = vi.fn();
    const addEventListener = vi.fn();
    const eventSource = vi.fn(function EventSourceMock() {
      return { addEventListener, close };
    });
    vi.stubGlobal('EventSource', eventSource);

    const subscription = openPmaChatEventSource({ onEvent: vi.fn() }, '/car');

    expect(eventSource).toHaveBeenCalledWith('/car/hub/pma/events', {
      withCredentials: undefined
    });
    expect(addEventListener).toHaveBeenCalledWith('chat_snapshot', expect.any(Function));
    subscription.close();
    expect(close).toHaveBeenCalledOnce();
  });

  it('opens generic chat surface EventSource under the configured hub base path', () => {
    const close = vi.fn();
    const addEventListener = vi.fn();
    const eventSource = vi.fn(function EventSourceMock() {
      return { addEventListener, close };
    });
    vi.stubGlobal('EventSource', eventSource);

    const subscription = openChatSurfaceEventSource({ onEvent: vi.fn() }, '/car');

    expect(eventSource).toHaveBeenCalledWith('/car/hub/chat/events', {
      withCredentials: undefined
    });
    expect(addEventListener).toHaveBeenCalledWith('chat.snapshot', expect.any(Function));
    expect(addEventListener).toHaveBeenCalledWith('chat.event', expect.any(Function));
    subscription.close();
    expect(close).toHaveBeenCalledOnce();
  });

  it('persists chat surface cursors and reconnects with backoff', () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal('localStorage', memoryStorage());
    const events: unknown[] = [];

    const subscription = openChatSurfaceEventSource({ onEvent: (event) => events.push(event) }, '/car');
    expect(FakeEventSource.instances[0].url).toBe('/car/hub/chat/events');

    FakeEventSource.instances[0].emit('chat.event', { event_type: 'surface.updated' }, '22');
    expect(localStorage.getItem('car.stream.cursor.chat.surface')).toBe('22');
    FakeEventSource.instances[0].fail();
    expect(FakeEventSource.instances[0].closed).toBe(true);

    vi.advanceTimersByTime(500);
    expect(FakeEventSource.instances[1].url).toBe('/car/hub/chat/events?cursor=22');
    expect(events).toHaveLength(1);
    subscription.close();
  });

  it('resumes flow run streams by persisted event id after interruption', () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal('localStorage', memoryStorage());
    const events: unknown[] = [];

    const subscription = openFlowRunEventSource('run-1', { repo: 'repo-1' }, { onEvent: (event) => events.push(event) }, '/car');
    expect(FakeEventSource.instances[0].url).toBe('/car/repos/repo-1/api/flows/run-1/events');

    FakeEventSource.instances[0].emit('message', { status: 'running' }, '7');
    expect(localStorage.getItem('car.stream.cursor.flow.repo-1.run-1')).toBe('7');
    FakeEventSource.instances[0].fail();
    vi.advanceTimersByTime(500);

    expect(FakeEventSource.instances[1].url).toBe('/car/repos/repo-1/api/flows/run-1/events?after=7');
    expect(events).toEqual([{ id: '7', payload: { status: 'running' } }]);
    subscription.close();
  });
});

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => {
      values.set(key, value);
    }
  };
}
