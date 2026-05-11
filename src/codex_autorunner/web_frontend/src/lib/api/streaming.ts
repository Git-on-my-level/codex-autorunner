import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';

export type SseEvent<T = unknown> = {
  id: string | null;
  event: string;
  data: T;
  retry: number | null;
};

export type PmaTailStreamEvent =
  | { kind: 'state'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'tail'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'timeline'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'progress'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'message'; payload: unknown; lastEventId: string | null };

export type PmaChatStreamEvent =
  | { kind: 'chat_snapshot'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'message'; payload: unknown; lastEventId: string | null };

export type ChatSurfaceStreamEvent =
  | { kind: 'chat_snapshot'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'chat_event'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'message'; payload: unknown; lastEventId: string | null };

export type StreamSubscription = {
  close: () => void;
};

export type FlowRunStreamEvent = {
  id: string | null;
  payload: Record<string, unknown>;
};

export type JsonStreamOptions = {
  onEvent: (event: PmaTailStreamEvent) => void;
  onError?: (error: Event) => void;
  onStatus?: (status: 'connecting' | 'connected' | 'interrupted' | 'closed') => void;
  withCredentials?: boolean;
};

export type PmaChatStreamOptions = {
  onEvent: (event: PmaChatStreamEvent) => void;
  onError?: (error: Event) => void;
  withCredentials?: boolean;
};

export type ChatSurfaceStreamOptions = {
  onEvent: (event: ChatSurfaceStreamEvent) => void;
  onError?: (error: Event) => void;
  onStatus?: (status: 'connecting' | 'connected' | 'interrupted' | 'closed') => void;
  withCredentials?: boolean;
};

export function parseSseFrame(frame: string): SseEvent<string> | null {
  const lines = frame.split(/\r?\n/);
  let id: string | null = null;
  let event = 'message';
  let retry: number | null = null;
  const data: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'id') id = value;
    else if (field === 'event') event = value || 'message';
    else if (field === 'retry') retry = parseRetry(value);
    else if (field === 'data') data.push(value);
  }

  if (!data.length && event === 'message' && id === null && retry === null) return null;
  return { id, event, data: data.join('\n'), retry };
}

export function parseJsonSseFrame(frame: string): SseEvent<unknown> | null {
  const parsed = parseSseFrame(frame);
  if (!parsed) return null;
  if (!parsed.data) return { ...parsed, data: null };
  try {
    return { ...parsed, data: JSON.parse(parsed.data) };
  } catch {
    return parsed;
  }
}

export function normalizePmaTailStreamEvent(event: SseEvent<unknown>): PmaTailStreamEvent {
  const payload = asRecord(event.data);
  if (event.event === 'state') return { kind: 'state', payload, lastEventId: event.id };
  if (event.event === 'tail') return { kind: 'tail', payload, lastEventId: event.id };
  if (event.event === 'timeline') return { kind: 'timeline', payload, lastEventId: event.id };
  if (event.event === 'progress') return { kind: 'progress', payload, lastEventId: event.id };
  return { kind: 'message', payload: event.data, lastEventId: event.id };
}

export function normalizePmaChatStreamEvent(event: SseEvent<unknown>): PmaChatStreamEvent {
  if (event.event === 'chat_snapshot') {
    return { kind: 'chat_snapshot', payload: asRecord(event.data), lastEventId: event.id };
  }
  return { kind: 'message', payload: event.data, lastEventId: event.id };
}

export function normalizeChatSurfaceStreamEvent(event: SseEvent<unknown>): ChatSurfaceStreamEvent {
  if (event.event === 'chat.snapshot') {
    return { kind: 'chat_snapshot', payload: asRecord(event.data), lastEventId: event.id };
  }
  if (event.event === 'chat.event') {
    return { kind: 'chat_event', payload: asRecord(event.data), lastEventId: event.id };
  }
  return { kind: 'message', payload: event.data, lastEventId: event.id };
}

export function openPmaTailEventSource(
  managedThreadId: string,
  options: JsonStreamOptions,
  basePath = runtimeBasePath()
): StreamSubscription {
  const encoded = encodeURIComponent(managedThreadId);
  let closed = false;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  const handle = (message: MessageEvent) => {
    attempt = 0;
    options.onStatus?.('connected');
    options.onEvent(
      normalizePmaTailStreamEvent({
        id: message.lastEventId || null,
        event: message.type || 'message',
        data: parseJson(message.data),
        retry: null
      })
    );
  };
  const connect = () => {
    if (closed) return;
    options.onStatus?.('connecting');
    source = new EventSource(withRuntimeBasePath(`/hub/pma/threads/${encoded}/tail/events`, basePath), {
      withCredentials: options.withCredentials
    });
    source.addEventListener('state', handle);
    source.addEventListener('tail', handle);
    source.addEventListener('timeline', handle);
    source.addEventListener('progress', handle);
    source.addEventListener('message', handle);
    source.addEventListener('error', (event) => {
      if (closed) return;
      options.onStatus?.('interrupted');
      options.onError?.(event);
      source?.close();
      const delay = Math.min(8000, 500 * 2 ** attempt);
      attempt += 1;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    });
  };
  connect();
  return {
    close: () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      source?.close();
      options.onStatus?.('closed');
    }
  };
}

export function openPmaChatEventSource(
  options: PmaChatStreamOptions,
  basePath = runtimeBasePath()
): StreamSubscription {
  const source = new EventSource(withRuntimeBasePath('/hub/pma/events', basePath), {
    withCredentials: options.withCredentials
  });
  const handle = (message: MessageEvent) => {
    options.onEvent(
      normalizePmaChatStreamEvent({
        id: message.lastEventId || null,
        event: message.type || 'message',
        data: parseJson(message.data),
        retry: null
      })
    );
  };
  source.addEventListener('chat_snapshot', handle);
  source.addEventListener('message', handle);
  source.addEventListener('error', (event) => options.onError?.(event));
  return { close: () => source.close() };
}

export function openChatSurfaceEventSource(
  options: ChatSurfaceStreamOptions,
  basePath = runtimeBasePath()
): StreamSubscription {
  let closed = false;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  const storageKey = 'car.stream.cursor.chat.surface';
  const handle = (message: MessageEvent) => {
    attempt = 0;
    options.onStatus?.('connected');
    if (message.lastEventId) rememberCursor(storageKey, message.lastEventId);
    options.onEvent(
      normalizeChatSurfaceStreamEvent({
        id: message.lastEventId || null,
        event: message.type || 'message',
        data: parseJson(message.data),
        retry: null
      })
    );
  };
  const connect = () => {
    if (closed) return;
    options.onStatus?.('connecting');
    const cursor = readCursor(storageKey);
    const path = cursor ? `/hub/chat/events?cursor=${encodeURIComponent(cursor)}` : '/hub/chat/events';
    source = new EventSource(withRuntimeBasePath(path, basePath), {
      withCredentials: options.withCredentials
    });
    source.addEventListener('chat.snapshot', handle);
    source.addEventListener('chat.event', handle);
    source.addEventListener('message', handle);
    source.addEventListener('error', (event) => {
      if (closed) return;
      options.onStatus?.('interrupted');
      options.onError?.(event);
      source?.close();
      const delay = Math.min(8000, 500 * 2 ** attempt);
      attempt += 1;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    });
  };
  connect();
  return {
    close: () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      source?.close();
      options.onStatus?.('closed');
    }
  };
}

export function openFlowRunEventSource(
  runId: string,
  owner: { repo?: string; worktree?: string } | undefined,
  options: {
    onEvent: (event: FlowRunStreamEvent) => void;
    onError?: (error: Event) => void;
    withCredentials?: boolean;
  },
  basePath = runtimeBasePath()
): StreamSubscription {
  const workspaceId = owner?.repo ?? owner?.worktree;
  const prefix = workspaceId ? `/repos/${encodeURIComponent(workspaceId)}/api/flows` : '/api/flows';
  const storageKey = `car.stream.cursor.flow.${workspaceId ?? 'hub'}.${runId}`;
  let closed = false;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  const connect = () => {
    if (closed) return;
    const after = readCursor(storageKey);
    const query = after ? `?after=${encodeURIComponent(after)}` : '';
    source = new EventSource(withRuntimeBasePath(`${prefix}/${encodeURIComponent(runId)}/events${query}`, basePath), {
      withCredentials: options.withCredentials
    });
    source.addEventListener('message', (message: MessageEvent) => {
      attempt = 0;
      if (message.lastEventId) rememberCursor(storageKey, message.lastEventId);
      options.onEvent({ id: message.lastEventId || null, payload: asRecord(parseJson(message.data)) });
    });
    source.addEventListener('error', (event) => {
      if (closed) return;
      options.onError?.(event);
      source?.close();
      const delay = Math.min(8000, 500 * 2 ** attempt);
      attempt += 1;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    });
  };
  connect();
  return {
    close: () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      source?.close();
    }
  };
}

function parseRetry(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function parseJson(value: string): unknown {
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readCursor(key: string): string | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage.getItem(key);
  } catch {
    return null;
  }
}

function rememberCursor(key: string, cursor: string): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, cursor);
  } catch {
    // Cursor persistence is best-effort; streams still resume through browser Last-Event-ID within one connection.
  }
}
