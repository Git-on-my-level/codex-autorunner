import {
  EventSourceStreamRuntime,
  storage,
  type CursorStorage,
  type EventSourceFactory
} from '$lib/runtime/eventSourceRuntime';
import type { StreamVisibilityPolicy } from '$lib/runtime/streamVisibilityPolicy';
import { parseJsonSseFrame, type SseEvent, type StreamSubscription } from '$lib/api/streaming';

export type ReadModelStreamStatus = 'idle' | 'connecting' | 'connected' | 'interrupted' | 'closed';

export type { CursorStorage, EventSourceFactory };

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
  visibilityPolicy?: StreamVisibilityPolicy | null;
  onResume?: () => void | Promise<void>;
};

export class ReadModelStreamManager<T> implements StreamSubscription {
  private readonly runtime: EventSourceStreamRuntime;

  constructor(private readonly options: ReadModelStreamOptions<T>) {
    this.runtime = new EventSourceStreamRuntime({
      path: () => this.pathWithCursor(),
      eventTypes: options.eventTypes,
      basePath: options.basePath,
      reconnectBaseMs: options.reconnectBaseMs,
      reconnectMaxMs: options.reconnectMaxMs,
      withCredentials: options.withCredentials,
      eventSourceFactory: options.eventSourceFactory,
      visibilityPolicy: options.visibilityPolicy,
      onResume: options.onResume,
      onStatus: options.onStatus,
      onError: options.onError,
      onMessage: (message) => this.handleMessage(message)
    });
  }

  open(): void {
    this.runtime.open();
  }

  close(): void {
    this.runtime.close();
  }

  cursor(): string | null {
    return storage(this.options.cursorStorage).getItem(cursorKey(this.options.key));
  }

  resetCursor(): void {
    storage(this.options.cursorStorage).removeItem(cursorKey(this.options.key));
  }

  private pathWithCursor(): string {
    const currentCursor = this.cursor();
    const params = new URLSearchParams();
    if (currentCursor) params.set('cursor', currentCursor);
    const query = params.toString();
    if (!query) return this.options.path;
    return `${this.options.path}${this.options.path.includes('?') ? '&' : '?'}${query}`;
  }

  private handleMessage(message: MessageEvent): void {
    const parsed = parseEventSourceMessage(message);
    const mapped = this.options.parse(parsed);
    const cursor = message.lastEventId || parsed.id;
    if (cursor) storage(this.options.cursorStorage).setItem(cursorKey(this.options.key), cursor);
    if (mapped) this.options.onEvent(mapped, cursor);
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
