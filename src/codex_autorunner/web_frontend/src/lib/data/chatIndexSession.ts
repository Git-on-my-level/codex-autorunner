import { writable, type Readable } from 'svelte/store';
import { pmaApi, type ApiError, type JsonRecord, type PmaApiClient } from '$lib/api/client';
import {
  openChatSurfaceEventSource,
  type ChatSurfaceStreamEvent,
  type ChatSurfaceStreamOptions,
  type StreamSubscription
} from '$lib/api/streaming';
import {
  legacyChatIndexRecordToChatIndexRow,
  pmaChatCounters,
  pmaChatSummaryToChatIndexRow,
  selectPmaChats,
  syntheticProjectionCursor
} from './readModelViewModels';
import { readModelEntityStore, type ReadModelEntityStore } from './readModelStore';
import {
  mapChatSurfaceSnapshotToPmaChats,
  reconcileChatSurfaceEvent,
  reconcileChatSurfaceSnapshot
} from '$lib/viewModels/pmaChat';

export type ChatIndexSessionStatus = 'idle' | 'loading' | 'connected' | 'interrupted' | 'closed';

export type ChatIndexSessionState = {
  status: ChatIndexSessionStatus;
  error: ApiError | null;
};

export type ChatIndexSession = {
  state: Readable<ChatIndexSessionState>;
  start: () => void;
  stop: () => void;
  refresh: () => Promise<void>;
  isStarted: () => boolean;
};

type ChatIndexSessionDeps = {
  api?: PmaApiClient;
  store?: ReadModelEntityStore;
  openStream?: (options: ChatSurfaceStreamOptions) => StreamSubscription;
};

export function createChatIndexSession(deps: ChatIndexSessionDeps = {}): ChatIndexSession {
  const api = deps.api ?? pmaApi;
  const store = deps.store ?? readModelEntityStore;
  const openStream = deps.openStream ?? openChatSurfaceEventSource;
  const state = writable<ChatIndexSessionState>({ status: 'idle', error: null });
  let streamSubscription: StreamSubscription | null = null;
  let refreshPromise: Promise<void> | null = null;

  function start(): void {
    if (streamSubscription) return;
    const cached = Boolean(store.snapshot().chatIndexCursor);
    if (!cached) void refresh();
    streamSubscription = openStream({
      onEvent: (event) => handleStreamEvent(event),
      onError: () => {
        state.set({ status: 'interrupted', error: null });
        void refresh();
      },
      onStatus: (status) => {
        if (status === 'connected') state.set({ status: 'connected', error: null });
        else if (status === 'interrupted') state.set({ status: 'interrupted', error: null });
        else if (status === 'closed') state.set({ status: 'closed', error: null });
      }
    });
  }

  function stop(): void {
    streamSubscription?.close();
    streamSubscription = null;
    state.set({ status: 'closed', error: null });
  }

  async function refresh(): Promise<void> {
    if (refreshPromise) return refreshPromise;
    state.set({ status: 'loading', error: null });
    refreshPromise = refreshChatList()
      .then(() => {
        state.set({ status: streamSubscription ? 'connected' : 'idle', error: null });
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
    const [activeChatResult, archivedChatResult] = await Promise.all([
      api.getJson<JsonRecord>('/hub/chat/index?view=all&limit=200'),
      api.getJson<JsonRecord>('/hub/chat/index?view=archived&limit=200')
    ]);
    if (!activeChatResult.ok) throw activeChatResult.error;
    const rows = [
      ...asRecords(activeChatResult.data.rows).map(legacyChatIndexRecordToChatIndexRow),
      ...(archivedChatResult.ok ? asRecords(archivedChatResult.data.rows).map(legacyChatIndexRecordToChatIndexRow) : [])
    ];
    store.replaceChatIndexRows(rows, syntheticProjectionCursor('pma.thread-list.session'));
  }

  function handleStreamEvent(event: ChatSurfaceStreamEvent): void {
    if (event.kind === 'chat_snapshot') {
      const currentChats = selectPmaChats(store.snapshot());
      const nextChats = mapChatSurfaceSnapshotToPmaChats(event.payload);
      const reconciled = reconcileChatSurfaceSnapshot(currentChats, nextChats, null);
      replacePmaChatList(preserveExistingChatOrder(currentChats, reconciled.chats));
      return;
    }
    if (event.kind === 'chat_event') {
      replacePmaChatList(reconcileChatSurfaceEvent(selectPmaChats(store.snapshot()), event.payload));
    }
  }

  function replacePmaChatList(nextChats: ReturnType<typeof selectPmaChats>): void {
    store.replaceChatIndexRows(
      nextChats.map(pmaChatSummaryToChatIndexRow),
      syntheticProjectionCursor('pma.chat-list.session'),
      pmaChatCounters(nextChats)
    );
  }

  return {
    state,
    start,
    stop,
    refresh,
    isStarted: () => streamSubscription !== null
  };
}

export const chatIndexSession = createChatIndexSession();

function preserveExistingChatOrder(currentChats: ReturnType<typeof selectPmaChats>, nextChats: ReturnType<typeof selectPmaChats>): ReturnType<typeof selectPmaChats> {
  if (!currentChats.length) return nextChats;
  const nextById = new Map(nextChats.map((chat) => [chat.id, chat]));
  const ordered: ReturnType<typeof selectPmaChats> = [];
  const seen = new Set<string>();
  for (const current of currentChats) {
    const next = nextById.get(current.id);
    if (!next) continue;
    ordered.push(next);
    seen.add(next.id);
  }
  for (const next of nextChats) {
    if (seen.has(next.id)) continue;
    ordered.push(next);
  }
  return ordered;
}

function asRecords(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : [];
}
