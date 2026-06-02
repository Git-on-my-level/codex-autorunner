import { describe, expect, it } from 'vitest';
import {
  CHAT_DRAFT_STORAGE_KEY,
  clearChatDraft,
  draftRecordHasContent,
  draftRecordIsLocalDraft,
  loadChatDraftRecords,
  pruneEmptyDraft,
  saveChatDraftRecords,
  setChatDraftAttachments,
  setChatDraftText,
  sortedChatDraftRecords,
  upsertDraftShell,
  type ChatDraftStorage
} from './chatDraftStore';
import type { PendingAttachment } from '$lib/viewModels/chat';
import type { ChatSummary } from '$lib/viewModels/domain';

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

function attachment(id: string): PendingAttachment {
  return {
    id,
    kind: 'image',
    title: `${id}.png`,
    sizeLabel: '12 KB',
    url: null,
    uploadedName: `${id}.png`,
    uploadState: 'uploaded'
  };
}

function draftChat(id: string): ChatSummary {
  return {
    id,
    title: 'New chat',
    lifecycleStatus: 'draft',
    status: 'idle',
    agentId: null,
    agentProfile: null,
    model: null,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    isTicketFlow: false,
    progressPercent: null,
    updatedAt: '2026-05-27T10:00:00.000Z',
    raw: { draft: true }
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
        attachments: [],
        updatedAt: '2026-05-27T10:00:00.000Z',
        chatSnapshot: null
      }
    });
  });

  it('persists draft attachments alongside text and round-trips them', () => {
    const storage = memoryStorage();
    const records = setChatDraftAttachments(
      setChatDraftText({}, 'chat-a', 'see screenshot', null, '2026-05-27T10:00:00.000Z'),
      'chat-a',
      [attachment('shot-1')],
      null,
      '2026-05-27T10:05:00.000Z'
    );

    saveChatDraftRecords(records, storage);

    const loaded = loadChatDraftRecords(storage);
    expect(loaded['chat-a'].text).toBe('see screenshot');
    expect(loaded['chat-a'].attachments).toEqual([attachment('shot-1')]);
  });

  it('keeps an attachment-only draft (no text) as content-bearing', () => {
    const records = setChatDraftAttachments({}, 'chat-a', [attachment('shot-1')]);

    expect(draftRecordHasContent(records['chat-a'])).toBe(true);
    expect(sortedChatDraftRecords(records).map((record) => record.chatId)).toEqual(['chat-a']);
  });

  it('does not delete a draft when its text is cleared but attachments remain', () => {
    const withBoth = setChatDraftAttachments(
      setChatDraftText({}, 'chat-a', 'hello'),
      'chat-a',
      [attachment('shot-1')]
    );
    const textCleared = setChatDraftText(withBoth, 'chat-a', '');

    expect(textCleared['chat-a'].attachments).toEqual([attachment('shot-1')]);
    expect(draftRecordHasContent(textCleared['chat-a'])).toBe(true);
  });

  it('upserts an empty shell that is not persisted until content is added', () => {
    const storage = memoryStorage();
    const records = upsertDraftShell({}, draftChat('chat-a'), '2026-05-27T10:00:00.000Z');

    expect(records['chat-a'].chatSnapshot?.id).toBe('chat-a');
    expect(draftRecordHasContent(records['chat-a'])).toBe(false);
    expect(draftRecordIsLocalDraft(records['chat-a'])).toBe(true);

    saveChatDraftRecords(records, storage);
    expect(storage.getItem(CHAT_DRAFT_STORAGE_KEY)).toBeNull();
  });

  it('does not classify ordinary saved composer text as a local unsent draft', () => {
    const records = setChatDraftText({}, 'chat-a', 'follow-up');

    expect(draftRecordHasContent(records['chat-a'])).toBe(true);
    expect(draftRecordIsLocalDraft(records['chat-a'])).toBe(false);
  });

  it('upsertDraftShell preserves text typed against the same id', () => {
    const typed = setChatDraftText({}, 'chat-a', 'work in progress');
    const reshelled = upsertDraftShell(typed, draftChat('chat-a'));

    expect(reshelled['chat-a'].text).toBe('work in progress');
    expect(reshelled['chat-a'].chatSnapshot?.id).toBe('chat-a');
  });

  it('prunes only empty drafts', () => {
    const records = {
      ...upsertDraftShell({}, draftChat('empty')),
      ...setChatDraftText({}, 'has-text', 'keep me')
    };

    const pruned = pruneEmptyDraft(records, 'empty');
    expect(pruned.empty).toBeUndefined();

    expect(pruneEmptyDraft(records, 'has-text')).toBe(records);
  });

  it('migrates v1 payloads (no attachments) to v2 records', () => {
    const storage = memoryStorage({
      [CHAT_DRAFT_STORAGE_KEY]: JSON.stringify({
        version: 1,
        drafts: [{ chatId: 'chat-a', text: 'legacy', updatedAt: '2026-05-27T10:00:00.000Z' }]
      })
    });

    expect(loadChatDraftRecords(storage)).toEqual({
      'chat-a': {
        chatId: 'chat-a',
        text: 'legacy',
        attachments: [],
        updatedAt: '2026-05-27T10:00:00.000Z',
        chatSnapshot: null
      }
    });
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
