import type { ChatSummary } from '$lib/viewModels/domain';
import { normalizePendingAttachments, type PendingAttachment } from '$lib/viewModels/chat';

export const CHAT_DRAFT_STORAGE_KEY = 'car.webHub.chatDrafts.v1';

export type ChatDraftRecord = {
  chatId: string;
  text: string;
  attachments: PendingAttachment[];
  updatedAt: string;
  chatSnapshot?: ChatSummary | null;
};

export type ChatDraftRecordMap = Record<string, ChatDraftRecord>;

export type ChatDraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

type StoredDraftPayload = {
  version: 1 | 2;
  drafts: ChatDraftRecord[];
};

/** A draft is worth surfacing/persisting once it carries a message or an attachment. */
export function draftRecordHasContent(record: ChatDraftRecord | null | undefined): boolean {
  if (!record) return false;
  return record.text.trim().length > 0 || record.attachments.length > 0;
}

export function draftRecordIsLocalDraft(record: ChatDraftRecord | null | undefined): boolean {
  if (!record) return false;
  return record.chatSnapshot?.lifecycleStatus === 'draft' || record.chatSnapshot?.raw?.draft === true;
}

export function loadChatDraftRecords(storage = browserDraftStorage()): ChatDraftRecordMap {
  if (!storage) return {};
  try {
    const raw = storage.getItem(CHAT_DRAFT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<StoredDraftPayload>;
    if ((parsed.version !== 1 && parsed.version !== 2) || !Array.isArray(parsed.drafts)) return {};
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
  // Empty shells live only in memory; only content-bearing drafts are persisted.
  const drafts = sortedChatDraftRecords(records);
  try {
    if (drafts.length === 0) {
      storage.removeItem(CHAT_DRAFT_STORAGE_KEY);
      return;
    }
    const payload: StoredDraftPayload = { version: 2, drafts };
    storage.setItem(CHAT_DRAFT_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Draft persistence is best-effort; the live composer remains usable.
  }
}

/**
 * Create or refresh the empty shell for a just-minted draft chat, preserving any
 * text/attachments already typed against the same id. The shell carries the
 * chat summary so the sidebar and composer can render the draft after a reload
 * or after navigating away and back.
 */
export function upsertDraftShell(
  records: ChatDraftRecordMap,
  chat: ChatSummary,
  updatedAt = new Date().toISOString()
): ChatDraftRecordMap {
  const chatId = chat.id?.trim();
  if (!chatId) return records;
  const existing = records[chatId];
  return {
    ...records,
    [chatId]: {
      chatId,
      text: existing?.text ?? '',
      attachments: existing?.attachments ?? [],
      updatedAt: existing?.updatedAt ?? updatedAt,
      chatSnapshot: chat
    }
  };
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
  const existing = records[normalizedChatId];
  const next = { ...records };
  next[normalizedChatId] = {
    chatId: normalizedChatId,
    text,
    attachments: existing?.attachments ?? [],
    updatedAt,
    chatSnapshot: chatSnapshot ?? existing?.chatSnapshot ?? null
  };
  return next;
}

export function setChatDraftAttachments(
  records: ChatDraftRecordMap,
  chatId: string | null | undefined,
  attachments: PendingAttachment[],
  chatSnapshot?: ChatSummary | null,
  updatedAt = new Date().toISOString()
): ChatDraftRecordMap {
  const normalizedChatId = chatId?.trim();
  if (!normalizedChatId) return records;
  const existing = records[normalizedChatId];
  const next = { ...records };
  next[normalizedChatId] = {
    chatId: normalizedChatId,
    text: existing?.text ?? '',
    attachments: [...attachments],
    updatedAt,
    chatSnapshot: chatSnapshot ?? existing?.chatSnapshot ?? null
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

/** Drop an abandoned draft that never accumulated any text or attachments. */
export function pruneEmptyDraft(
  records: ChatDraftRecordMap,
  chatId: string | null | undefined
): ChatDraftRecordMap {
  const normalizedChatId = chatId?.trim();
  if (!normalizedChatId) return records;
  const existing = records[normalizedChatId];
  if (!existing || draftRecordHasContent(existing)) return records;
  return clearChatDraft(records, normalizedChatId);
}

export function sortedChatDraftRecords(records: ChatDraftRecordMap): ChatDraftRecord[] {
  return Object.values(records)
    .filter((record) => draftRecordHasContent(record))
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

function normalizeDraftRecord(raw: unknown): ChatDraftRecord | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Partial<ChatDraftRecord>;
  const chatId = typeof record.chatId === 'string' ? record.chatId.trim() : '';
  const text = typeof record.text === 'string' ? record.text : '';
  const attachments = normalizePendingAttachments(record.attachments);
  if (!chatId) return null;
  if (!text.trim() && attachments.length === 0) return null;
  return {
    chatId,
    text,
    attachments,
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
