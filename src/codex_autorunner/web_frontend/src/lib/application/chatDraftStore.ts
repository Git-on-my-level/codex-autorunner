import type { ChatSummary } from '$lib/viewModels/domain';

export const CHAT_DRAFT_STORAGE_KEY = 'car.webHub.chatDrafts.v1';

export type ChatDraftRecord = {
  chatId: string;
  text: string;
  updatedAt: string;
  chatSnapshot?: ChatSummary | null;
};

export type ChatDraftRecordMap = Record<string, ChatDraftRecord>;

export type ChatDraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

type StoredDraftPayload = {
  version: 1;
  drafts: ChatDraftRecord[];
};

export function loadChatDraftRecords(storage = browserDraftStorage()): ChatDraftRecordMap {
  if (!storage) return {};
  try {
    const raw = storage.getItem(CHAT_DRAFT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<StoredDraftPayload>;
    if (parsed.version !== 1 || !Array.isArray(parsed.drafts)) return {};
    const records: ChatDraftRecordMap = {};
    for (const record of parsed.drafts) {
      const normalized = normalizeDraftRecord(record);
      if (normalized) records[normalized.chatId] = normalized;
    }
    return records;
  } catch {
    return {};
  }
}

export function saveChatDraftRecords(
  records: ChatDraftRecordMap,
  storage = browserDraftStorage()
): void {
  if (!storage) return;
  const drafts = sortedChatDraftRecords(records);
  try {
    if (drafts.length === 0) {
      storage.removeItem(CHAT_DRAFT_STORAGE_KEY);
      return;
    }
    const payload: StoredDraftPayload = { version: 1, drafts };
    storage.setItem(CHAT_DRAFT_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Draft persistence is best-effort; the live composer remains usable.
  }
}

export function setChatDraftText(
  records: ChatDraftRecordMap,
  chatId: string | null | undefined,
  text: string,
  chatSnapshot?: ChatSummary | null,
  updatedAt = new Date().toISOString()
): ChatDraftRecordMap {
  const normalizedChatId = chatId?.trim();
  if (!normalizedChatId) return records;
  const trimmedText = text.trim();
  const next = { ...records };
  if (!trimmedText) {
    delete next[normalizedChatId];
    return next;
  }
  next[normalizedChatId] = {
    chatId: normalizedChatId,
    text,
    updatedAt,
    chatSnapshot: chatSnapshot ?? records[normalizedChatId]?.chatSnapshot ?? null
  };
  return next;
}

export function clearChatDraft(
  records: ChatDraftRecordMap,
  chatId: string | null | undefined
): ChatDraftRecordMap {
  const normalizedChatId = chatId?.trim();
  if (!normalizedChatId || !records[normalizedChatId]) return records;
  const next = { ...records };
  delete next[normalizedChatId];
  return next;
}

export function sortedChatDraftRecords(records: ChatDraftRecordMap): ChatDraftRecord[] {
  return Object.values(records)
    .filter((record) => record.text.trim())
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

function normalizeDraftRecord(raw: unknown): ChatDraftRecord | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Partial<ChatDraftRecord>;
  const chatId = typeof record.chatId === 'string' ? record.chatId.trim() : '';
  const text = typeof record.text === 'string' ? record.text : '';
  if (!chatId || !text.trim()) return null;
  return {
    chatId,
    text,
    updatedAt: typeof record.updatedAt === 'string' && record.updatedAt.trim()
      ? record.updatedAt
      : new Date().toISOString(),
    chatSnapshot: record.chatSnapshot ?? null
  };
}

function browserDraftStorage(): ChatDraftStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}
