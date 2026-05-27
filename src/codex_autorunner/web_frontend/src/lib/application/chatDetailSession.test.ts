import { describe, expect, it } from 'vitest';
import type { PmaChatSummary } from '$lib/viewModels/domain';
import type { ChatListEntry } from '$lib/viewModels/pmaChat';
import {
  activateChatDetail,
  activateRequestedChatFromRows,
  chatSummaryForSessionId,
  clearCommittedDraftPlaceholderIfPersisted,
  initialChatDetailSessionState,
  loadPinnedChats,
  markSessionChatRead,
  PINNED_CHATS_STORAGE_KEY,
  replacementForArchivedActiveChat,
  savePinnedChats,
  sortEntriesForPinnedChats,
  startLocalDraftChat,
  togglePinnedChatId
} from './chatDetailSession';

const now = '2026-05-16T12:00:00.000Z';

describe('chat detail session', () => {
  it('activates a deep-linked chat before the chat index has loaded', () => {
    const command = activateChatDetail(initialChatDetailSessionState(), {
      detailId: 'deep-linked-chat',
      chats: [],
      hasCachedDetail: () => false
    });

    expect(command.state).toMatchObject({
      activeChatId: 'deep-linked-chat',
      detailMode: 'detail',
      loadingActive: true,
      activeError: null
    });
    expect(command.runtime).toEqual({ chatId: 'deep-linked-chat', quiet: false });
    expect(command.markRead).toBe(false);
  });

  it('does not replace a deep-linked chat with the first loaded row while the requested row is absent', () => {
    const state = {
      ...initialChatDetailSessionState(),
      activeChatId: 'deep-linked-chat',
      detailMode: 'detail' as const
    };

    const command = activateRequestedChatFromRows(state, {
      loadedChats: [chat({ id: 'visible-chat' })],
      requestedChatId: 'deep-linked-chat',
      hasCachedDetail: () => false
    });

    expect(command.state.activeChatId).toBe('deep-linked-chat');
    expect(command.runtime).toBeNull();
    expect(command.syncUrl).toBe(false);
  });

  it('chooses a sibling replacement when the active chat is archived out of the active window', () => {
    const previous = [
      chat({ id: 'active', status: 'running', raw: { surface_kind: 'managed_thread', surface_key: 'scope-1' } }),
      chat({ id: 'other', repoId: 'repo-2', worktreeId: 'wt-2' })
    ];
    const next = [
      chat({ id: 'active', lifecycleStatus: 'archived', raw: { surface_kind: 'managed_thread', surface_key: 'scope-1' } }),
      chat({ id: 'replacement', status: 'idle', raw: { surface_kind: 'managed_thread', surface_key: 'scope-1' } })
    ];

    expect(replacementForArchivedActiveChat(previous, next, 'active')).toBe('replacement');
  });

  it('resolves a client-minted draft chat while the active chat window is stale', () => {
    const draft = chat({
      id: 'pma:11111111-1111-4111-8111-111111111111',
      title: 'New chat',
      lifecycleStatus: 'draft',
      raw: { draft: true }
    });
    const started = startLocalDraftChat(initialChatDetailSessionState(), draft);

    expect(chatSummaryForSessionId('pma:11111111-1111-4111-8111-111111111111', [], started.localDraftChat)).toMatchObject({
      id: 'pma:11111111-1111-4111-8111-111111111111',
      title: 'New chat'
    });
  });

  it('persists read markers through an injected adapter', () => {
    const saved: Array<Record<string, string>> = [];
    const next = markSessionChatRead(
      {},
      'chat-1',
      [chat({ id: 'chat-1', updatedAt: now })],
      null,
      { save: (markers) => saved.push(markers) }
    );

    expect(next).toEqual({ 'chat-1': now });
    expect(saved).toEqual([{ 'chat-1': now }]);
  });

  it('loads, toggles, persists, and applies pin-aware sidebar ordering', () => {
    const storage = memoryStorage();
    storage.setItem(PINNED_CHATS_STORAGE_KEY, JSON.stringify(['chat-2']));
    const pinned = togglePinnedChatId(loadPinnedChats(storage), 'chat-1');
    savePinnedChats(pinned, storage);

    expect(JSON.parse(storage.getItem(PINNED_CHATS_STORAGE_KEY) ?? '[]')).toEqual(['chat-1', 'chat-2']);

    const entries: ChatListEntry[] = [
      { kind: 'chat', chat: chat({ id: 'chat-3', updatedAt: '2026-05-16T10:00:00.000Z' }) },
      { kind: 'chat', chat: chat({ id: 'chat-2', updatedAt: '2026-05-16T09:00:00.000Z' }) },
      { kind: 'chat', chat: chat({ id: 'chat-1', updatedAt: '2026-05-16T08:00:00.000Z' }) }
    ];

    expect(sortEntriesForPinnedChats(entries, pinned).map((entry) => (entry.kind === 'chat' ? entry.chat.id : entry.group.key))).toEqual([
      'chat-2',
      'chat-1',
      'chat-3'
    ]);
  });
});

function chat(overrides: Partial<PmaChatSummary>): PmaChatSummary {
  return {
    id: 'chat-1',
    title: 'Chat',
    lifecycleStatus: null,
    status: 'idle',
    agentId: null,
    agentProfile: null,
    model: null,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    isTicketFlow: false,
    progressPercent: null,
    updatedAt: now,
    raw: {},
    ...overrides
  };
}

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    }
  };
}
