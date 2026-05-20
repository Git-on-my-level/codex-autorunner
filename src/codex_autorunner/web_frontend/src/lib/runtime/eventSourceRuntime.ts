import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';
import {
  alwaysLiveStreamVisibilityPolicy,
  type StreamVisibilityPolicy
} from '$lib/runtime/streamVisibilityPolicy';

export type EventSourceStreamStatus = 'connecting' | 'connected' | 'interrupted' | 'closed';

export type EventSourceFactory = (url: string, init?: EventSourceInit) => EventSource;

export type CursorStorage = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
};

export type EventSourceStreamRuntimeOptions = {
  path: string | (() => string);
  eventTypes: string[];
  onMessage: (message: MessageEvent) => void;
  onError?: (error: Event) => void;
  onStatus?: (status: EventSourceStreamStatus) => void;
  onResume?: () => void;
  onBeforeReconnect?: () => void;
  basePath?: string;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  withCredentials?: boolean;
  eventSourceFactory?: EventSourceFactory;
  visibilityPolicy?: StreamVisibilityPolicy | null;
};

export class EventSourceStreamRuntime {
  private source: EventSource | null = null;
  private closed = true;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private unsubscribeVisibility: (() => void) | null = null;

  constructor(private readonly options: EventSourceStreamRuntimeOptions) {}

  open(): void {
    if (!this.closed) return;
    this.closed = false;
    this.subscribeVisibility();
    if (this.shouldSuspendForHiddenTab()) {
      this.options.onStatus?.('interrupted');
      return;
    }
    this.connect();
  }

  close(): void {
    if (this.closed && !this.source && !this.reconnectTimer) return;
    this.closed = true;
    this.clearReconnectTimer();
    this.closeSource();
    this.unsubscribeVisibility?.();
    this.unsubscribeVisibility = null;
    this.options.onStatus?.('closed');
  }

  private connect(): void {
    if (this.closed) return;
    if (this.shouldSuspendForHiddenTab()) return;
    this.closeSource();
    this.options.onStatus?.('connecting');
    const source = this.sourceFactory()(this.url(), { withCredentials: this.options.withCredentials });
    this.source = source;
    const handle = (message: MessageEvent) => {
      if (source !== this.source) return;
      this.attempt = 0;
      this.options.onStatus?.('connected');
      this.options.onMessage(message);
    };
    for (const eventType of this.options.eventTypes) {
      if (eventType !== 'message') source.addEventListener(eventType, handle);
    }
    source.addEventListener('message', handle);
    source.addEventListener('error', (event) => {
      if (source !== this.source) return;
      this.options.onStatus?.('interrupted');
      this.options.onError?.(event);
      this.closeSource();
      if (!this.closed) this.scheduleReconnect();
    });
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer();
    if (this.shouldSuspendForHiddenTab()) return;
    this.options.onBeforeReconnect?.();
    const base = this.options.reconnectBaseMs ?? 500;
    const max = this.options.reconnectMaxMs ?? 8000;
    const delay = Math.min(max, base * 2 ** this.attempt);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private subscribeVisibility(): void {
    if (this.unsubscribeVisibility) return;
    const policy = this.visibilityPolicy();
    if (!policy.suspendWhenHidden) return;
    this.unsubscribeVisibility = policy.subscribe((visible) => {
      if (this.closed) return;
      if (!visible) {
        this.clearReconnectTimer();
        this.closeSource();
        this.options.onStatus?.('interrupted');
        return;
      }
      this.options.onResume?.();
      this.connect();
    });
  }

  private shouldSuspendForHiddenTab(): boolean {
    const policy = this.visibilityPolicy();
    return policy.suspendWhenHidden && !policy.isVisible();
  }

  private visibilityPolicy(): StreamVisibilityPolicy {
    return this.options.visibilityPolicy ?? alwaysLiveStreamVisibilityPolicy;
  }

  private sourceFactory(): EventSourceFactory {
    return this.options.eventSourceFactory ?? ((url, init) => new EventSource(url, init));
  }

  private url(): string {
    const path = typeof this.options.path === 'function' ? this.options.path() : this.options.path;
    return withRuntimeBasePath(path, this.options.basePath ?? runtimeBasePath());
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }
}

export function storage(candidate: CursorStorage | null | undefined): CursorStorage {
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
