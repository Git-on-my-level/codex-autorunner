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
  let streamCursor = 0;

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
      streamCursor = Math.max(streamCursor, streamCursorFromPayload(event.payload), streamCursorFromId(event.lastEventId));
      return;
    }
    if (event.kind === 'chat_event') {
      const eventCursor = streamCursorFromPayload(event.payload) || streamCursorFromId(event.lastEventId);
      if (eventCursor > 0 && eventCursor <= streamCursor) return;
      const currentChats = selectPmaChats(store.snapshot());
      const nextChats = reconcileChatSurfaceEvent(currentChats, event.payload);
      replacePmaChatList(preserveMetadataOnlyEventActivity(currentChats, nextChats, event.payload));
      if (eventCursor > 0) streamCursor = Math.max(streamCursor, eventCursor);
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

function preserveMetadataOnlyEventActivity(
  currentChats: ReturnType<typeof selectPmaChats>,
  nextChats: ReturnType<typeof selectPmaChats>,
  eventPayload: JsonRecord
): ReturnType<typeof selectPmaChats> {
  if (chatEventMovesRows(eventPayload)) return nextChats;
  const currentById = new Map(currentChats.map((chat) => [chat.id, chat]));
  return nextChats.map((chat) => {
    const current = currentById.get(chat.id);
    if (!current || !current.updatedAt) return chat;
    return {
      ...chat,
      updatedAt: current.updatedAt,
      raw: {
        ...chat.raw,
        last_activity_at: current.updatedAt
      }
    };
  });
}

function chatEventMovesRows(eventPayload: JsonRecord): boolean {
  const eventType = typeof eventPayload.event_type === 'string' ? eventPayload.event_type : '';
  return !new Set([
    'channel_directory.discovered',
    'notification.reply_context_changed',
    'surface.bound',
    'surface.rebound'
  ]).has(eventType);
}

function streamCursorFromPayload(payload: JsonRecord): number {
  return streamCursorFromUnknown(payload.cursor);
}

function streamCursorFromId(id: string | null): number {
  return streamCursorFromUnknown(id);
}

function streamCursorFromUnknown(value: unknown): number {
  const raw = typeof value === 'number' ? String(value) : typeof value === 'string' ? value.trim() : '';
  if (!/^\d+$/.test(raw)) return 0;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
}

function asRecords(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : [];
}
