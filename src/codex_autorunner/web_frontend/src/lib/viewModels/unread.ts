import type { PmaChatSummary } from './domain';

const STORAGE_KEY = 'pma:lastSeen';

export type ChatLastSeenMap = Record<string, string>;

export function loadLastSeenMap(): ChatLastSeenMap {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const out: ChatLastSeenMap = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof key === 'string' && typeof value === 'string') out[key] = value;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveLastSeenMap(map: ChatLastSeenMap): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Best-effort; ignore quota / disabled storage.
  }
}

/** Stamp `chatId` as read at `at` (or now). Returns a new map; safe to assign back. */
export function markChatRead(
  map: ChatLastSeenMap,
  chatId: string,
  at: string | null = null
): ChatLastSeenMap {
  if (!chatId) return map;
  const stamp = at ?? new Date().toISOString();
  const existing = map[chatId];
  if (existing && existing >= stamp) return map;
  return { ...map, [chatId]: stamp };
}

export function isChatUnread(chat: PmaChatSummary, map: ChatLastSeenMap): boolean {
  if (!chat.updatedAt) return false;
  const seen = map[chat.id];
  if (!seen) return true;
  return chat.updatedAt > seen;
}

/** Mark every chat that is unread under `baseMap` (uses each chat's `updatedAt` or now). */
export function markAllChatsRead(
  baseMap: ChatLastSeenMap,
  chats: PmaChatSummary[]
): ChatLastSeenMap {
  let next = baseMap;
  const now = new Date().toISOString();
  for (const chat of chats) {
    if (!isChatUnread(chat, baseMap)) continue;
    const stamp = chat.updatedAt ?? now;
    next = markChatRead(next, chat.id, stamp);
  }
  return next;
}
