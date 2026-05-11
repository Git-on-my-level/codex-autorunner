import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';
import { parseJsonSseFrame, type SseEvent, type StreamSubscription } from '$lib/api/streaming';

export type ReadModelStreamStatus = 'idle' | 'connecting' | 'connected' | 'interrupted' | 'closed';

export type CursorStorage = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
};

export type EventSourceFactory = (url: string, init?: EventSourceInit) => EventSource;

export type ReadModelStreamOptions<T> = {
  key: string;
  path: string;
  eventTypes: string[];
  parse: (event: SseEvent<unknown>) => T | null;
  onEvent: (event: T, cursor: string | null) => void;
  onStatus?: (status: ReadModelStreamStatus) => void;
  onError?: (error: Event) => void;
  cursorStorage?: CursorStorage | null;
  eventSourceFactory?: EventSourceFactory;
  basePath?: string;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  withCredentials?: boolean;
};

export class ReadModelStreamManager<T> implements StreamSubscription {
  private source: EventSource | null = null;
  private closed = false;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: ReadModelStreamOptions<T>) {}

  open(): void {
    this.closed = false;
    this.connect();
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
    this.options.onStatus?.('closed');
  }

  cursor(): string | null {
    return storage(this.options.cursorStorage).getItem(cursorKey(this.options.key));
  }

  resetCursor(): void {
    storage(this.options.cursorStorage).removeItem(cursorKey(this.options.key));
  }

  private connect(): void {
    if (this.closed) return;
    this.options.onStatus?.('connecting');
    const currentCursor = this.cursor();
    const params = new URLSearchParams();
    if (currentCursor) params.set('cursor', currentCursor);
    const path = `${this.options.path}${this.options.path.includes('?') ? '&' : '?'}${params.toString()}`;
    const url = withRuntimeBasePath(path.endsWith('?') ? path.slice(0, -1) : path, this.options.basePath ?? runtimeBasePath());
    const factory = this.options.eventSourceFactory ?? ((sourceUrl, init) => new EventSource(sourceUrl, init));
    const source = factory(url, { withCredentials: this.options.withCredentials });
    this.source = source;
    const handle = (message: MessageEvent) => {
      this.attempt = 0;
      this.options.onStatus?.('connected');
      const parsed = parseEventSourceMessage(message);
      const mapped = this.options.parse(parsed);
      const cursor = message.lastEventId || parsed.id;
      if (cursor) storage(this.options.cursorStorage).setItem(cursorKey(this.options.key), cursor);
      if (mapped) this.options.onEvent(mapped, cursor);
    };
    for (const eventType of this.options.eventTypes) source.addEventListener(eventType, handle);
    source.addEventListener('message', handle);
    source.addEventListener('error', (event) => {
      this.options.onStatus?.('interrupted');
      this.options.onError?.(event);
      source.close();
      if (!this.closed) this.scheduleReconnect();
    });
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    const base = this.options.reconnectBaseMs ?? 500;
    const max = this.options.reconnectMaxMs ?? 8000;
    const delay = Math.min(max, base * 2 ** this.attempt);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}

export function openReadModelStream<T>(options: ReadModelStreamOptions<T>): ReadModelStreamManager<T> {
  const manager = new ReadModelStreamManager(options);
  manager.open();
  return manager;
}

function parseEventSourceMessage(message: MessageEvent): SseEvent<unknown> {
  if (typeof message.data === 'string' && message.data.includes('\n')) {
    return parseJsonSseFrame(message.data) ?? {
      id: message.lastEventId || null,
      event: message.type || 'message',
      data: null,
      retry: null
    };
  }
  let data: unknown = message.data;
  if (typeof message.data === 'string' && message.data.trim()) {
    try {
      data = JSON.parse(message.data);
    } catch {
      data = message.data;
    }
  }
  return {
    id: message.lastEventId || null,
    event: message.type || 'message',
    data,
    retry: null
  };
}

function cursorKey(key: string): string {
  return `car.readModel.cursor.${key}`;
}

function storage(candidate: CursorStorage | null | undefined): CursorStorage {
  if (candidate) return candidate;
  if (typeof localStorage !== 'undefined') return localStorage;
  return memoryStorage;
}

const memory = new Map<string, string>();
const memoryStorage: CursorStorage = {
  getItem: (key) => memory.get(key) ?? null,
  setItem: (key, value) => {
    memory.set(key, value);
  },
  removeItem: (key) => {
    memory.delete(key);
  }
};
