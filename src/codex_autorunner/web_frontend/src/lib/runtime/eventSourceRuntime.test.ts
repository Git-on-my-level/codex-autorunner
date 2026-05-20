import { describe, expect, it, vi } from 'vitest';
import type { StreamVisibilityPolicy } from './streamVisibilityPolicy';
import { EventSourceStreamRuntime } from './eventSourceRuntime';

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
}

describe('event source stream runtime', () => {
  it('opens once, reconnects with a fresh path, and closes idempotently', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    let cursor: string | null = null;
    const statuses: string[] = [];
    const runtime = new EventSourceStreamRuntime({
      path: () => (cursor ? `/events?cursor=${cursor}` : '/events'),
      eventTypes: ['patch'],
      basePath: '/car',
      reconnectBaseMs: 10,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      onStatus: (status) => statuses.push(status),
      onMessage: (message) => {
        cursor = message.lastEventId || null;
      }
    });

    runtime.open();
    runtime.open();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/car/events');

    FakeEventSource.instances[0].emit('patch', { ok: true }, '42');
    FakeEventSource.instances[0].fail();
    vi.advanceTimersByTime(10);

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toBe('/car/events?cursor=42');
    runtime.close();
    runtime.close();
    expect(statuses).toEqual(['connecting', 'connected', 'interrupted', 'connecting', 'closed']);
    vi.useRealTimers();
  });

  it('ignores stale source failures after a reconnect has replaced the source', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const onError = vi.fn();
    const runtime = new EventSourceStreamRuntime({
      path: '/events',
      eventTypes: ['patch'],
      reconnectBaseMs: 10,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      onError,
      onMessage: () => {}
    });

    runtime.open();
    const stale = FakeEventSource.instances[0];
    stale.fail();
    vi.advanceTimersByTime(10);
    stale.fail();

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(onError).toHaveBeenCalledOnce();
    runtime.close();
    vi.useRealTimers();
  });

  it('suspends hidden streams and resumes with the latest path', async () => {
    FakeEventSource.instances = [];
    const visibilityPolicy = new FakeVisibilityPolicy();
    visibilityPolicy.setVisible(false);
    let cursor: string | null = 'seed';
    const runtime = new EventSourceStreamRuntime({
      path: () => `/events?cursor=${cursor}`,
      eventTypes: ['patch'],
      visibilityPolicy,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      onResume: () => {
        cursor = 'resumed';
      },
      onMessage: () => {}
    });

    runtime.open();
    expect(FakeEventSource.instances).toHaveLength(0);

    visibilityPolicy.setVisible(true);
    await Promise.resolve();

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/events?cursor=resumed');
    runtime.close();
  });

  it('schedules reconnect instead of leaking state when the source factory throws', () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const statuses: string[] = [];
    const onError = vi.fn();
    const runtime = new EventSourceStreamRuntime({
      path: '/events',
      eventTypes: ['patch'],
      reconnectBaseMs: 10,
      eventSourceFactory: vi
        .fn()
        .mockImplementationOnce(() => {
          throw new Error('constructor failed');
        })
        .mockImplementation((url, init) => new FakeEventSource(url, init) as unknown as EventSource),
      onStatus: (status) => statuses.push(status),
      onError,
      onMessage: () => {}
    });

    runtime.open();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(onError).toHaveBeenCalledOnce();
    expect(statuses).toEqual(['connecting', 'interrupted']);

    vi.advanceTimersByTime(10);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/events');
    runtime.close();
    vi.useRealTimers();
  });

  it('waits for async resume work before reconnecting a visible stream', async () => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    const visibilityPolicy = new FakeVisibilityPolicy();
    let resolveResume!: () => void;
    let cursor = 'old';
    const runtime = new EventSourceStreamRuntime({
      path: () => `/events?cursor=${cursor}`,
      eventTypes: ['patch'],
      visibilityPolicy,
      eventSourceFactory: (url, init) => new FakeEventSource(url, init) as unknown as EventSource,
      onResume: () =>
        new Promise<void>((resolve) => {
          resolveResume = () => {
            cursor = 'fresh';
            resolve();
          };
        }),
      onMessage: () => {}
    });

    runtime.open();
    expect(FakeEventSource.instances).toHaveLength(1);
    visibilityPolicy.setVisible(false);
    visibilityPolicy.setVisible(true);
    expect(FakeEventSource.instances).toHaveLength(1);

    resolveResume();
    await Promise.resolve();

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toBe('/events?cursor=fresh');
    runtime.close();
    vi.useRealTimers();
  });
});
