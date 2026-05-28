import type { ChatSummary } from './domain';

const STORAGE_KEYS = ['pma:lastSeen', 'car.web.chat.lastSeen.v1'];

export type ChatLastSeenMap = Record<string, string>;

export function loadLastSeenMap(): ChatLastSeenMap {
  if (typeof window === 'undefined') return {};
  const out: ChatLastSeenMap = {};
  try {
    for (const storageKey of STORAGE_KEYS) {
      const raw = window.localStorage.getItem(storageKey);
      if (!raw) continue;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') continue;
      for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof key !== 'string' || typeof value !== 'string') continue;
        if (!out[key] || value > out[key]) out[key] = value;
      }
    }
    if (Object.keys(out).length > 0) saveLastSeenMap(out);
    return out;
  } catch {
    return out;
  }
}

export function saveLastSeenMap(map: ChatLastSeenMap): void {
  if (typeof window === 'undefined') return;
  try {
    const raw = JSON.stringify(map);
    for (const storageKey of STORAGE_KEYS) window.localStorage.setItem(storageKey, raw);
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

export function isChatUnread(chat: ChatSummary, map: ChatLastSeenMap): boolean {
  if (!chat.updatedAt) return false;
  const seen = map[chat.id];
  if (!seen) return true;
  return chat.updatedAt > seen;
}

/** Mark every chat that is unread under `baseMap` (uses each chat's `updatedAt` or now). */
export function markAllChatsRead(
  baseMap: ChatLastSeenMap,
  chats: ChatSummary[]
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
