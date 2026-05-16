import { writable, type Readable } from 'svelte/store';
import type { ApiError } from '$lib/api/client';
import { mapReadModelContract, type ChatIndexPatchEvent } from '$lib/api/readModelContracts';
import type { SseEvent } from '$lib/api/streaming';
import {
  readModelSnapshotClient,
  type ChatIndexRequest,
  type ReadModelSnapshotClient
} from './readModelClients';
import { canonicalChatIndexWindowKey, readModelEntityStore, type ReadModelEntityStore } from './readModelStore';
import { openReadModelStream, type CursorStorage, type ReadModelStreamManager, type ReadModelStreamOptions } from './readModelStream';

export type ChatIndexSessionStatus = 'idle' | 'loading' | 'connected' | 'interrupted' | 'closed';

export type ChatIndexSessionState = {
  status: ChatIndexSessionStatus;
  error: ApiError | null;
};

export type ChatIndexSession = {
  state: Readable<ChatIndexSessionState>;
  start: () => void;
  stop: () => void;
  refresh: (request?: ChatIndexRequest) => Promise<void>;
  isStarted: () => boolean;
};

type ChatIndexSessionDeps = {
  client?: ReadModelSnapshotClient;
  store?: ReadModelEntityStore;
  streamFactory?: ChatIndexStreamFactory;
};

export type ChatIndexStreamFactory = (options: ReadModelStreamOptions<ChatIndexPatchEvent>) => ReadModelStreamManager<ChatIndexPatchEvent>;

export function createChatIndexSession(deps: ChatIndexSessionDeps = {}): ChatIndexSession {
  const client = deps.client ?? readModelSnapshotClient;
  const store = deps.store ?? readModelEntityStore;
  const streamFactory = deps.streamFactory ?? openReadModelStream<ChatIndexPatchEvent>;
  const state = writable<ChatIndexSessionState>({ status: 'idle', error: null });
  let refreshPromise: Promise<void> | null = null;
  let started = false;
  let stream: ReadModelStreamManager<ChatIndexPatchEvent> | null = null;
  let currentRequest: ChatIndexRequest = { filter: 'all', limit: 50 };
  let inFlightRequest: ChatIndexRequest | null = null;
  let refreshAgain = false;

  function start(): void {
    if (started) return;
    started = true;
    const cached = Boolean(store.snapshot().chatIndexCursor);
    if (!cached) {
      void refresh();
      return;
    }
    openStream();
  }

  function stop(): void {
    started = false;
    stream?.close();
    stream = null;
    state.set({ status: 'closed', error: null });
  }

  async function refresh(request: ChatIndexRequest = currentRequest): Promise<void> {
    currentRequest = { ...currentRequest, ...request };
    if (refreshPromise) {
      if (!sameChatIndexRequest(inFlightRequest, currentRequest)) refreshAgain = true;
      return refreshPromise;
    }
    state.set({ status: 'loading', error: null });
    refreshPromise = refreshChatListUntilSettled()
      .then(() => {
        state.set({ status: started ? 'connected' : 'idle', error: null });
        if (started && !stream) openStream();
      })
      .catch((error: ApiError) => {
        state.set({ status: 'interrupted', error });
      })
      .finally(() => {
        inFlightRequest = null;
        refreshAgain = false;
        refreshPromise = null;
      });
    return refreshPromise;
  }

  async function refreshChatListUntilSettled(): Promise<void> {
    do {
      refreshAgain = false;
      inFlightRequest = { ...currentRequest };
      const result = await client.chatIndex(inFlightRequest);
      if (!result.ok) throw result.error;
      store.applyChatIndexSnapshot(result.data, inFlightRequest);
    } while (refreshAgain || !sameChatIndexRequest(inFlightRequest, currentRequest));
  }

  function openStream(): void {
    if (stream) return;
    stream = streamFactory({
      key: 'chat.index.entity',
      path: '/hub/read-models/chats/patches',
      eventTypes: ['chat.index.patch', 'projection.cursor_gap'],
      cursorStorage: seededCursorStorage(
        store.snapshot().chatIndexCursor?.sequence
          ? String(store.snapshot().chatIndexCursor?.sequence)
          : null
      ),
      parse: parseChatIndexPatchEvent,
      onEvent: (event) => {
        const result = store.applyChatIndexPatchEvent(event);
        if (result === 'repair_required') {
          stream?.resetCursor();
          void refresh();
          return;
        }
        if (result === 'applied') {
          const key = canonicalChatIndexWindowKey(currentRequest);
          const window = store.snapshot().chatWindows[key];
          if (window?.status === 'interrupted') void refresh(currentRequest);
        }
      },
      onStatus: (status) => {
        if (status === 'connecting') state.set({ status: 'loading', error: null });
        if (status === 'connected') state.set({ status: 'connected', error: null });
        if (status === 'interrupted') state.set({ status: 'interrupted', error: null });
      }
    });
  }

  return {
    state,
    start,
    stop,
    refresh,
    isStarted: () => started
  };
}

export const chatIndexSession = createChatIndexSession();

const fallbackCursorMemory = new Map<string, string>();
const fallbackCursorStorage: CursorStorage = {
  getItem: (key) => fallbackCursorMemory.get(key) ?? null,
  setItem: (key, value) => {
    fallbackCursorMemory.set(key, value);
  },
  removeItem: (key) => {
    fallbackCursorMemory.delete(key);
  }
};

function seededCursorStorage(seed: string | null): CursorStorage {
  const backing = typeof localStorage !== 'undefined' ? localStorage : fallbackCursorStorage;
  return {
    getItem: (key) => {
      const current = backing.getItem(key);
      if (cursorSequence(current) >= cursorSequence(seed)) return current;
      return seed;
    },
    setItem: (key, value) => backing.setItem(key, value),
    removeItem: (key) => backing.removeItem(key)
  };
}

function cursorSequence(value: string | null): number {
  if (!value) return 0;
  const raw = value.startsWith('chat.index:') ? value.slice('chat.index:'.length) : value;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function parseChatIndexPatchEvent(event: SseEvent<unknown>): ChatIndexPatchEvent | null {
  if (event.event !== 'chat.index.patch' && event.event !== 'projection.cursor_gap' && event.event !== 'message') {
    return null;
  }
  return mapReadModelContract<ChatIndexPatchEvent>(event.data);
}

function sameChatIndexRequest(left: ChatIndexRequest | null, right: ChatIndexRequest | null): boolean {
  if (!left || !right) return left === right;
  return (
    (left.filter ?? 'all') === (right.filter ?? 'all') &&
    (left.limit ?? 200) === (right.limit ?? 200) &&
    (left.query ?? '') === (right.query ?? '') &&
    (left.surfaceKind ?? '') === (right.surfaceKind ?? '')
  );
}
