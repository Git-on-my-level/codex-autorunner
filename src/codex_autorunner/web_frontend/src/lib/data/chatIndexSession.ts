import { writable, type Readable } from 'svelte/store';
import type { ApiError } from '$lib/api/client';
import type { ChatIndexCounters, ChatIndexRow, ChatIndexSnapshot } from '$lib/api/readModelContracts';
import {
  readModelSnapshotClient,
  type ReadModelSnapshotClient
} from './readModelClients';
import { readModelEntityStore, type ReadModelEntityStore } from './readModelStore';

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
  client?: ReadModelSnapshotClient;
  store?: ReadModelEntityStore;
};

/** Same merge semantics as merging two sequential index windows (last occurrence wins order). */
function mergeUniqueChatIndexRows(primary: ChatIndexRow[], secondary: ChatIndexRow[]): ChatIndexRow[] {
  const order: string[] = [];
  const byChatId = new Map<string, ChatIndexRow>();
  for (const row of [...primary, ...secondary]) {
    if (!byChatId.has(row.chatId)) order.push(row.chatId);
    byChatId.set(row.chatId, row);
  }
  return order.map((id) => byChatId.get(id)).filter((row): row is ChatIndexRow => Boolean(row));
}

export function createChatIndexSession(deps: ChatIndexSessionDeps = {}): ChatIndexSession {
  const client = deps.client ?? readModelSnapshotClient;
  const store = deps.store ?? readModelEntityStore;
  const state = writable<ChatIndexSessionState>({ status: 'idle', error: null });
  let refreshPromise: Promise<void> | null = null;
  let started = false;

  function start(): void {
    if (started) return;
    started = true;
    const cached = Boolean(store.snapshot().chatIndexCursor);
    if (!cached) void refresh();
  }

  function stop(): void {
    started = false;
    state.set({ status: 'closed', error: null });
  }

  async function refresh(): Promise<void> {
    if (refreshPromise) return refreshPromise;
    state.set({ status: 'loading', error: null });
    refreshPromise = refreshChatList()
      .then(() => {
        state.set({ status: started ? 'connected' : 'idle', error: null });
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
    const [activeRes, archivedRes] = await Promise.all([
      client.chatIndex({ filter: 'all', limit: 200 }),
      client.chatIndex({ filter: 'archived', limit: 200 })
    ]);
    if (!activeRes.ok) throw activeRes.error;
    const active = activeRes.data;
    const archivedRows = archivedRes.ok ? archivedRes.data.rows : [];
    const rows = mergeUniqueChatIndexRows(active.rows, archivedRows);
    store.applyChatIndexSnapshot({
      ...active,
      rows,
      counters: mergeCounters(active, archivedRes.ok ? archivedRes.data : null, rows)
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

function mergeCounters(
  active: ChatIndexSnapshot,
  archived: ChatIndexSnapshot | null,
  rows: ChatIndexRow[]
): ChatIndexCounters {
  if (!archived) return active.counters;
  return {
    total: rows.length,
    waiting: rows.filter((row) => row.status === 'waiting').length,
    running: rows.filter((row) => row.status === 'running').length,
    unread: rows.reduce((total, row) => total + Math.max(0, row.unreadCount), 0),
    archived: archived.counters.archived
  };
}
