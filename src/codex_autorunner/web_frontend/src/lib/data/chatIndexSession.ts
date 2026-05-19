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
  setCompanionRequests: (requests: ChatIndexRequest[]) => void;
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
  let companionRequests: ChatIndexRequest[] = [];
  let inFlightRequest: ChatIndexRequest | null = null;
  let inFlightCompanionRequests: ChatIndexRequest[] = [];
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

  function setCompanionRequests(requests: ChatIndexRequest[]): void {
    const next = uniqueChatIndexRequests(requests);
    if (sameChatIndexRequestList(companionRequests, next)) return;
    companionRequests = next;
    if (refreshPromise) {
      refreshAgain = true;
      return;
    }
    if (started) void refresh(currentRequest);
  }

  async function refresh(request: ChatIndexRequest = currentRequest): Promise<void> {
    currentRequest = { ...currentRequest, ...request };
    if (refreshPromise) {
      if (!sameChatIndexRequest(inFlightRequest, currentRequest) || !sameChatIndexRequestList(inFlightCompanionRequests, companionRequests)) refreshAgain = true;
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
        inFlightCompanionRequests = [];
        refreshAgain = false;
        refreshPromise = null;
      });
    return refreshPromise;
  }

  async function refreshChatListUntilSettled(): Promise<void> {
    do {
      refreshAgain = false;
      inFlightRequest = { ...currentRequest };
      inFlightCompanionRequests = companionRequests.map((request) => ({ ...request }));
      for (const request of requestsForRefresh(inFlightRequest, inFlightCompanionRequests)) {
        const result = await client.chatIndex(request);
        if (!result.ok) throw result.error;
        store.applyChatIndexSnapshot(result.data, request);
      }
    } while (
      refreshAgain ||
      !sameChatIndexRequest(inFlightRequest, currentRequest) ||
      !sameChatIndexRequestList(inFlightCompanionRequests, companionRequests)
    );
  }

  function openStream(): void {
    if (stream) return;
    stream = streamFactory({
      key: 'chat.index.entity',
      path: chatIndexPatchPath(currentRequest),
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
          const state = store.snapshot();
          const interrupted = requestsForRefresh(currentRequest, companionRequests)
            .some((request) => state.chatWindows[canonicalChatIndexWindowKey(request)]?.status === 'interrupted');
          if (interrupted) void refresh(currentRequest);
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
    setCompanionRequests,
    isStarted: () => started
  };
}

export const chatIndexSession = createChatIndexSession();

function chatIndexPatchPath(request: ChatIndexRequest): string {
  const params = new URLSearchParams({
    filter: request.filter ?? 'all'
  });
  if (request.query) params.set('search', request.query);
  if (request.surfaceKind) params.set('surface_kind', request.surfaceKind);
  if (request.groupBy) params.set('group_by', request.groupBy);
  if (request.parentGroupId) params.set('parent_group_id', request.parentGroupId);
  return `/hub/read-models/chats/patches?${params.toString()}`;
}

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
    (left.limit ?? 50) === (right.limit ?? 50) &&
    (left.query ?? '') === (right.query ?? '') &&
    (left.surfaceKind ?? '') === (right.surfaceKind ?? '') &&
    (left.groupBy ?? '') === (right.groupBy ?? '') &&
    (left.parentGroupId ?? '') === (right.parentGroupId ?? '')
  );
}

function sameChatIndexRequestList(left: ChatIndexRequest[], right: ChatIndexRequest[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((request, index) => sameChatIndexRequest(request, right[index] ?? null));
}

function uniqueChatIndexRequests(requests: ChatIndexRequest[]): ChatIndexRequest[] {
  const unique: ChatIndexRequest[] = [];
  for (const request of requests) {
    if (unique.some((existing) => sameChatIndexRequest(existing, request))) continue;
    unique.push({ ...request });
  }
  return unique;
}

function requestsForRefresh(primary: ChatIndexRequest, companions: ChatIndexRequest[]): ChatIndexRequest[] {
  return uniqueChatIndexRequests([primary, ...companions]);
}
