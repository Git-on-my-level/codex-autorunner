import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';
import {
  alwaysLiveStreamVisibilityPolicy,
  type StreamVisibilityPolicy
} from '$lib/runtime/streamVisibilityPolicy';

export type EventSourceStreamStatus = 'connecting' | 'connected' | 'interrupted' | 'closed';

export type EventSourceFactory = (url: string, init?: EventSourceInit) => EventSource;

export type HubBearerTokenProvider = () => string | null | undefined;

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
  onResume?: () => void | Promise<void>;
  onBeforeReconnect?: () => void;
  basePath?: string;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  withCredentials?: boolean;
  hubBearerTokenProvider?: HubBearerTokenProvider;
  fetcher?: typeof fetch;
  eventSourceFactory?: EventSourceFactory;
  visibilityPolicy?: StreamVisibilityPolicy | null;
};

export class EventSourceStreamRuntime {
  private source: EventSource | null = null;
  private fetchAbortController: AbortController | null = null;
  private closed = true;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private unsubscribeVisibility: (() => void) | null = null;
  private resumeSeq = 0;
  private connectionSeq = 0;

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
    const token = this.options.hubBearerTokenProvider?.()?.trim();
    if (token) {
      void this.connectFetchStream(token);
      return;
    }
    let source: EventSource;
    try {
      source = this.sourceFactory()(this.url(), { withCredentials: this.options.withCredentials });
    } catch (error) {
      this.options.onStatus?.('interrupted');
      this.options.onError?.(error instanceof Event ? error : new Event('error'));
      if (!this.closed) this.scheduleReconnect();
      return;
    }
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

  private async connectFetchStream(token: string): Promise<void> {
    const seq = ++this.connectionSeq;
    const controller = new AbortController();
    this.fetchAbortController = controller;
    try {
      const response = await this.fetcher()(this.url(), {
        headers: {
          accept: 'text/event-stream',
          authorization: `Bearer ${token}`
        },
        credentials: this.options.withCredentials ? 'include' : 'same-origin',
        signal: controller.signal
      });
      if (this.closed || seq !== this.connectionSeq) return;
      if (!response.ok || !response.body) {
        throw new Error(`Event stream failed with HTTP ${response.status}`);
      }
      this.attempt = 0;
      this.options.onStatus?.('connected');
      await this.readFetchStream(response.body, seq);
    } catch (error) {
      if (this.closed || seq !== this.connectionSeq || controller.signal.aborted) return;
      this.options.onStatus?.('interrupted');
      this.options.onError?.(error instanceof Event ? error : new Event('error'));
      if (!this.closed) this.scheduleReconnect();
    }
  }

  private async readFetchStream(stream: ReadableStream<Uint8Array>, seq: number): Promise<void> {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (!this.closed && seq === this.connectionSeq) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = this.drainFrames(buffer, seq);
      }
      buffer += decoder.decode();
      this.drainFrames(buffer, seq, true);
    } finally {
      reader.releaseLock();
    }
    if (!this.closed && seq === this.connectionSeq) {
      this.options.onStatus?.('interrupted');
      this.scheduleReconnect();
    }
  }

  private drainFrames(buffer: string, seq: number, flush = false): string {
    let remaining = buffer;
    while (!this.closed && seq === this.connectionSeq) {
      const match = /\r?\n\r?\n/.exec(remaining);
      if (!match) break;
      const frame = remaining.slice(0, match.index);
      remaining = remaining.slice(match.index + match[0].length);
      this.dispatchFetchFrame(frame, seq);
    }
    if (flush && remaining.trim()) {
      this.dispatchFetchFrame(remaining, seq);
      return '';
    }
    return remaining;
  }

  private dispatchFetchFrame(frame: string, seq: number): void {
    if (this.closed || seq !== this.connectionSeq) return;
    const parsed = parseSseFrame(frame);
    if (!parsed) return;
    if (parsed.event !== 'message' && !this.options.eventTypes.includes(parsed.event)) return;
    this.options.onMessage(
      new MessageEvent(parsed.event, {
        data: parsed.data,
        lastEventId: parsed.id
      })
    );
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
      void this.resumeFromVisibility();
    });
  }

  private async resumeFromVisibility(): Promise<void> {
    const seq = ++this.resumeSeq;
    await this.options.onResume?.();
    if (this.closed || seq !== this.resumeSeq || this.shouldSuspendForHiddenTab()) return;
    this.connect();
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

  private fetcher(): typeof fetch {
    return this.options.fetcher ?? fetch;
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
    this.connectionSeq += 1;
    this.fetchAbortController?.abort();
    this.fetchAbortController = null;
    this.source?.close();
    this.source = null;
  }
}

function parseSseFrame(frame: string): { id: string; event: string; data: string } | null {
  const lines = frame.split(/\r?\n/);
  let id = '';
  let event = 'message';
  const data: string[] = [];
  for (const line of lines) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'id') id = value;
    else if (field === 'event') event = value || 'message';
    else if (field === 'data') data.push(value);
  }
  if (!id && event === 'message' && data.length === 0) return null;
  return { id, event, data: data.join('\n') };
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
