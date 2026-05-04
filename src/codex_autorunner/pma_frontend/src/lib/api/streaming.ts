export type SseEvent<T = unknown> = {
  id: string | null;
  event: string;
  data: T;
  retry: number | null;
};

export type PmaTailStreamEvent =
  | { kind: 'state'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'tail'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'progress'; payload: Record<string, unknown>; lastEventId: string | null }
  | { kind: 'message'; payload: unknown; lastEventId: string | null };

export type StreamSubscription = {
  close: () => void;
};

export type JsonStreamOptions = {
  onEvent: (event: PmaTailStreamEvent) => void;
  onError?: (error: Event) => void;
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
  if (event.event === 'progress') return { kind: 'progress', payload, lastEventId: event.id };
  return { kind: 'message', payload: event.data, lastEventId: event.id };
}

export function openPmaTailEventSource(
  managedThreadId: string,
  options: JsonStreamOptions,
  basePath = ''
): StreamSubscription {
  const encoded = encodeURIComponent(managedThreadId);
  const source = new EventSource(`${basePath}/hub/pma/threads/${encoded}/tail/events`, {
    withCredentials: options.withCredentials
  });
  const handle = (message: MessageEvent) => {
    options.onEvent(
      normalizePmaTailStreamEvent({
        id: message.lastEventId || null,
        event: message.type || 'message',
        data: parseJson(message.data),
        retry: null
      })
    );
  };
  source.addEventListener('state', handle);
  source.addEventListener('tail', handle);
  source.addEventListener('progress', handle);
  source.addEventListener('message', handle);
  source.addEventListener('error', (event) => options.onError?.(event));
  return { close: () => source.close() };
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
