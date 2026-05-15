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
    if (refreshPromise) return refreshPromise;
    state.set({ status: 'loading', error: null });
    refreshPromise = refreshChatList()
      .then(() => {
        state.set({ status: started ? 'connected' : 'idle', error: null });
        if (started) openStream();
      })
      .catch((error: ApiError) => {
        state.set({ status: 'interrupted', error });
      })
      .finally(() => {
        refreshPromise = null;
      });
    return refreshPromise;
  }

  async function refreshChatList(): Promise<void> {
    const result = await client.chatIndex(currentRequest);
    if (!result.ok) throw result.error;
    store.applyChatIndexSnapshot(result.data);
  }

  function openStream(): void {
    stream?.close();
    const params = new URLSearchParams({
      filter: currentRequest.filter ?? 'all',
      window_limit: String(currentRequest.limit ?? 200)
    });
    if (currentRequest.query) params.set('search', currentRequest.query);
    if (currentRequest.surfaceKind) params.set('surface_kind', currentRequest.surfaceKind);
    stream = streamFactory({
      key: `chat.index.${params.toString()}`,
      path: `/hub/read-models/chats/patches?${params.toString()}`,
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
