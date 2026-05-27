import { describe, expect, it } from 'vitest';
import {
  CHAT_DRAFT_STORAGE_KEY,
  clearChatDraft,
  loadChatDraftRecords,
  saveChatDraftRecords,
  setChatDraftText,
  sortedChatDraftRecords,
  type ChatDraftStorage
} from './chatDraftStore';

function memoryStorage(initial: Record<string, string> = {}): ChatDraftStorage {
  const values = { ...initial };
  return {
    getItem: (key) => values[key] ?? null,
    setItem: (key, value) => {
      values[key] = value;
    },
    removeItem: (key) => {
      delete values[key];
    }
  };
}

describe('chatDraftStore', () => {
  it('persists non-empty drafts per chat id', () => {
    const storage = memoryStorage();
    const records = setChatDraftText({}, 'chat-a', 'hello', null, '2026-05-27T10:00:00.000Z');

    saveChatDraftRecords(records, storage);

    expect(loadChatDraftRecords(storage)).toEqual({
      'chat-a': {
        chatId: 'chat-a',
        text: 'hello',
        updatedAt: '2026-05-27T10:00:00.000Z',
        chatSnapshot: null
      }
    });
  });

  it('clears empty drafts and removes storage when no drafts remain', () => {
    const storage = memoryStorage();
    const withDraft = setChatDraftText({}, 'chat-a', 'hello');
    const cleared = setChatDraftText(withDraft, 'chat-a', '   ');

    saveChatDraftRecords(cleared, storage);

    expect(storage.getItem(CHAT_DRAFT_STORAGE_KEY)).toBeNull();
    expect(cleared).toEqual({});
  });

  it('keeps draft rows sorted by newest update', () => {
    const records = {
      ...setChatDraftText({}, 'chat-a', 'old', null, '2026-05-27T10:00:00.000Z'),
      ...setChatDraftText({}, 'chat-b', 'new', null, '2026-05-27T11:00:00.000Z')
    };

    expect(sortedChatDraftRecords(records).map((record) => record.chatId)).toEqual(['chat-b', 'chat-a']);
  });

  it('ignores corrupt storage payloads', () => {
    const storage = memoryStorage({ [CHAT_DRAFT_STORAGE_KEY]: '{ nope' });

    expect(loadChatDraftRecords(storage)).toEqual({});
  });

  it('clears one chat without touching another chat draft', () => {
    const records = setChatDraftText(
      setChatDraftText({}, 'chat-a', 'first'),
      'chat-b',
      'second'
    );

    expect(clearChatDraft(records, 'chat-a')).toEqual({
      'chat-b': expect.objectContaining({ chatId: 'chat-b', text: 'second' })
    });
  });
});
