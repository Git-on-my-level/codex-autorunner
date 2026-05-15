import { runtimeBasePath, withRuntimeBasePath } from '$lib/runtime/basePath';

export type SseEvent<T = unknown> = {
  id: string | null;
  event: string;
  data: T;
  retry: number | null;
};

export type ChatTranscriptStreamEvent =
  | { kind: 'transcript_snapshot'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'transcript_append'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'transcript_patch'; payload: Record<string, unknown>; lastEventId: string | null }
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

export type TranscriptStreamOptions = {
  onEvent: (event: ChatTranscriptStreamEvent) => void;
  onError?: (error: Event) => void;
  onStatus?: (status: 'connecting' | 'connected' | 'interrupted' | 'closed') => void;
  sinceEventId?: string | number | null;
  sinceManagedTurnId?: string | null;
  withCredentials?: boolean;
};

export type ChatTranscriptStreamUseState = {
  status?: string | null;
  queueDepth?: number | null;
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

export function normalizeChatTranscriptStreamEvent(event: SseEvent<unknown>): ChatTranscriptStreamEvent {
  const payload = asRecord(event.data);
  if (event.event === 'transcript.snapshot') return { kind: 'transcript_snapshot', payload, lastEventId: event.id };
  if (event.event === 'transcript.append') return { kind: 'transcript_append', payload, lastEventId: event.id };
  if (event.event === 'transcript.patch') return { kind: 'transcript_patch', payload, lastEventId: event.id };
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

export function openChatTranscriptEventSource(
  managedThreadId: string,
  options: TranscriptStreamOptions,
  basePath = runtimeBasePath()
): StreamSubscription {
  const encoded = encodeURIComponent(managedThreadId);
  let closed = false;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let lastEventId: string | null = optionalString(options.sinceEventId);
  let lastManagedTurnId: string | null = optionalString(options.sinceManagedTurnId);
  const handle = (message: MessageEvent) => {
    attempt = 0;
    const event = normalizeChatTranscriptStreamEvent({
      id: message.lastEventId || null,
      event: message.type || 'message',
      data: parseJson(message.data),
      retry: null
    });
    const managedTurnId = managedTurnIdFromPayload(event.payload);
    if (managedTurnId && managedTurnId !== lastManagedTurnId) {
      lastManagedTurnId = managedTurnId;
      lastEventId = null;
    }
    if (message.lastEventId) lastEventId = message.lastEventId;
    options.onStatus?.('connected');
    options.onEvent(event);
  };
  const connect = () => {
    if (closed) return;
    options.onStatus?.('connecting');
    const params = new URLSearchParams();
    if (lastEventId) {
      params.set('since_event_id', lastEventId);
      if (lastManagedTurnId) params.set('since_managed_turn_id', lastManagedTurnId);
    }
    const cursorQuery = params.size > 0 ? `?${params.toString()}` : '';
    source = new EventSource(withRuntimeBasePath(`/hub/pma/threads/${encoded}/transcript/events${cursorQuery}`, basePath), {
      withCredentials: options.withCredentials
    });
    source.addEventListener('transcript.snapshot', handle);
    source.addEventListener('transcript.append', handle);
    source.addEventListener('transcript.patch', handle);
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

export function shouldUseChatTranscriptStream(
  chat: ChatTranscriptStreamUseState | null | undefined,
  progress: ChatTranscriptStreamUseState | null | undefined,
  queuedTurns = 0
): boolean {
  if (queuedTurns > 0 || (progress?.queueDepth ?? 0) > 0) return true;
  return isActiveChatTranscriptStatus(progress?.status) || isActiveChatTranscriptStatus(chat?.status);
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
      if (cursor) forgetCursor(storageKey);
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

function optionalString(value: unknown): string | null {
  if (typeof value === 'string') return value.length > 0 ? value : null;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function isActiveChatTranscriptStatus(status: unknown): boolean {
  return status === 'running' || status === 'waiting' || status === 'blocked';
}

function managedTurnIdFromPayload(payload: unknown): string | null {
  const data = asRecord(payload);
  const direct = optionalString(data.managed_turn_id ?? data.managedTurnId);
  if (direct) return direct;
  const status = asRecord(data.status);
  const statusTurn = optionalString(status.managed_turn_id ?? status.managedTurnId);
  if (statusTurn) return statusTurn;
  const rows = Array.isArray(data.rows) ? data.rows : [];
  for (const row of rows) {
    const rowTurn = optionalString(asRecord(row).turn_id ?? asRecord(row).turnId);
    if (rowTurn) return rowTurn;
  }
  return null;
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

function forgetCursor(key: string): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.removeItem(key);
  } catch {
    // Cursor persistence is best-effort; reconnect can still fall back to a fresh snapshot.
  }
}
