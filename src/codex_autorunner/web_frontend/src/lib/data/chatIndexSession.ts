import { writable, type Readable } from 'svelte/store';
import type { ApiError } from '$lib/api/client';
import { mapReadModelContract, type ChatIndexPatchEvent } from '$lib/api/readModelContracts';
import type { SseEvent } from '$lib/api/streaming';
import {
  readModelSnapshotClient,
  type ChatIndexRequest,
  type ReadModelSnapshotClient
} from './readModelClients';
import { readModelEntityStore, type ReadModelEntityStore } from './readModelStore';
import { openReadModelStream, type ReadModelStreamManager, type ReadModelStreamOptions } from './readModelStream';

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
  let currentRequest: ChatIndexRequest = { filter: 'all', limit: 200 };
  let inFlightRequest: ChatIndexRequest | null = null;
  let refreshAgain = false;

  function start(): void {
    if (started) return;
    started = true;
    const cached = Boolean(store.snapshot().chatIndexCursor);
    if (!cached) void refresh();
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
      parse: parseChatIndexPatchEvent,
      onEvent: (event) => {
        const result = store.applyChatIndexPatchEvent(event);
        if (result === 'repair_required') {
          stream?.resetCursor();
          void refresh();
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
