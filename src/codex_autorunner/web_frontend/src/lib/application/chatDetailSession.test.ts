import { describe, expect, it } from 'vitest';
import type { ApiError } from '$lib/api/client';
import type { PmaChatSummary } from '$lib/viewModels/domain';
import type { ChatListEntry } from '$lib/viewModels/pmaChat';
import {
  activateChatDetailFromUrl,
  activateRequestedChatFromRows,
  chatSummaryForSessionId,
  clearCommittedDraftPlaceholderIfPersisted,
  commitLocalDraftChat,
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
    const command = activateChatDetailFromUrl(initialChatDetailSessionState(), {
      detailId: 'deep-linked-chat',
      chats: [],
      hasCachedDetail: () => false,
      activeDetailLoadResult: () => ({ status: 'cold', tags: [] })
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

  it('surfaces route-loader errors during deep-link activation', () => {
    const error: ApiError = { kind: 'http', status: 404, code: 'missing', message: 'missing' };
    const command = activateChatDetailFromUrl(initialChatDetailSessionState(), {
      detailId: 'missing-chat',
      chats: [],
      hasCachedDetail: () => false,
      activeDetailLoadResult: () => ({ status: 'error', tags: [], error })
    });

    expect(command.state.loadingActive).toBe(false);
    expect(command.state.activeError).toBe(error);
    expect(command.runtime).toEqual({ chatId: null, quiet: true });
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

  it('keeps a committed draft placeholder until the backend chat appears', () => {
    const draft = chat({
      id: 'draft:pma:1',
      title: 'New chat',
      lifecycleStatus: 'draft',
      raw: { draft: true }
    });
    const started = startLocalDraftChat(initialChatDetailSessionState(), draft);
    const committed = commitLocalDraftChat(started, draft, 'managed-thread-1', now);

    expect(committed).toMatchObject({
      activeChatId: 'managed-thread-1',
      pendingCommittedDetailUrlChatId: 'managed-thread-1',
      localDraftChat: null
    });
    expect(committed.committedDraftChat).toMatchObject({
      id: 'managed-thread-1',
      lifecycleStatus: 'active',
      raw: {
        draft_committed_placeholder: true,
        previous_draft_id: 'draft:pma:1',
        managed_thread_id: 'managed-thread-1'
      }
    });
    expect(clearCommittedDraftPlaceholderIfPersisted(committed, [chat({ id: 'other' })]).committedDraftChat?.id).toBe(
      'managed-thread-1'
    );
    expect(clearCommittedDraftPlaceholderIfPersisted(committed, [chat({ id: 'managed-thread-1' })]).committedDraftChat).toBeNull();
  });

  it('resolves a committed draft placeholder even when the active chat window is stale', () => {
    const draft = chat({
      id: 'draft:pma:1',
      title: 'New chat',
      lifecycleStatus: 'draft',
      raw: { draft: true }
    });
    const committed = commitLocalDraftChat(
      startLocalDraftChat(initialChatDetailSessionState(), draft),
      draft,
      'managed-thread-1',
      now
    );

    expect(chatSummaryForSessionId('managed-thread-1', [], null, committed.committedDraftChat)).toMatchObject({
      id: 'managed-thread-1',
      title: 'New chat',
      raw: {
        draft_committed_placeholder: true,
        managed_thread_id: 'managed-thread-1'
      }
    });
  });

  it('keeps a committed draft selected while the detail URL is still catching up', () => {
    const state = {
      ...initialChatDetailSessionState(),
      activeChatId: 'managed-thread-1',
      detailMode: 'detail' as const,
      pendingCommittedDetailUrlChatId: 'managed-thread-1'
    };

    const command = activateChatDetailFromUrl(state, {
      detailId: null,
      chats: [chat({ id: 'other' })],
      hasCachedDetail: () => false,
      activeDetailLoadResult: () => null
    });

    expect(command.state.activeChatId).toBe('managed-thread-1');
    expect(command.state.detailMode).toBe('detail');
    expect(command.state.pendingCommittedDetailUrlChatId).toBe('managed-thread-1');
    expect(command.runtime).toBeNull();
  });

  it('clears the committed draft URL guard once the committed detail route is visible', () => {
    const state = {
      ...initialChatDetailSessionState(),
      activeChatId: 'managed-thread-1',
      detailMode: 'detail' as const,
      pendingCommittedDetailUrlChatId: 'managed-thread-1'
    };

    const command = activateChatDetailFromUrl(state, {
      detailId: 'managed-thread-1',
      chats: [chat({ id: 'managed-thread-1' })],
      hasCachedDetail: () => true,
      activeDetailLoadResult: () => ({ status: 'cache-hit', tags: [] })
    });

    expect(command.state.activeChatId).toBe('managed-thread-1');
    expect(command.state.pendingCommittedDetailUrlChatId).toBeNull();
    expect(command.runtime).toBeNull();
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
