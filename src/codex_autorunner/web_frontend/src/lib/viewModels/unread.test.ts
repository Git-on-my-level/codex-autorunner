import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatSummary } from './domain';
import { isChatUnread, loadLastSeenMap, markAllChatsRead, saveLastSeenMap } from './unread';

describe('chat unread marker persistence', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('merges legacy and namespaced local storage keys and writes both back', () => {
    const storage = memoryStorage();
    storage.setItem('pma:lastSeen', JSON.stringify({ 'chat-1': '2026-05-11T10:00:00Z' }));
    storage.setItem(
      'car.web.chat.lastSeen.v1',
      JSON.stringify({
        'chat-1': '2026-05-11T11:00:00Z',
        'chat-2': '2026-05-11T09:00:00Z'
      })
    );
    vi.stubGlobal('window', { localStorage: storage });

    expect(loadLastSeenMap()).toEqual({
      'chat-1': '2026-05-11T11:00:00Z',
      'chat-2': '2026-05-11T09:00:00Z'
    });
    expect(JSON.parse(storage.getItem('pma:lastSeen') ?? '{}')).toEqual(
      JSON.parse(storage.getItem('car.web.chat.lastSeen.v1') ?? '{}')
    );
  });

  it('saves read markers to both stable storage keys', () => {
    const storage = memoryStorage();
    vi.stubGlobal('window', { localStorage: storage });

    saveLastSeenMap({ 'chat-1': '2026-05-11T10:00:00Z' });

    expect(storage.getItem('pma:lastSeen')).toBe('{"chat-1":"2026-05-11T10:00:00Z"}');
    expect(storage.getItem('car.web.chat.lastSeen.v1')).toBe('{"chat-1":"2026-05-11T10:00:00Z"}');
  });

  it('ignores backend unread counts and uses browser timestamp markers', () => {
    const unread = chat({ id: 'unread', unreadCount: 2, updatedAt: '2026-05-11T10:00:00Z' });
    const read = chat({ id: 'read', unreadCount: 0, updatedAt: '2026-05-11T11:00:00Z' });

    expect(isChatUnread(unread, { unread: '2026-05-11T10:00:00Z' })).toBe(false);
    expect(isChatUnread(read, {})).toBe(true);
  });

  it('marks only chats considered unread by local timestamp markers', () => {
    const next = markAllChatsRead(
      {},
      [
        chat({ id: 'backend-unread', unreadCount: 1, updatedAt: '2026-05-11T10:00:00Z' }),
        chat({ id: 'backend-read', unreadCount: 0, updatedAt: '2026-05-11T11:00:00Z' }),
        chat({ id: 'no-updated-at', unreadCount: 3, updatedAt: null })
      ]
    );

    expect(next).toEqual({
      'backend-unread': '2026-05-11T10:00:00Z',
      'backend-read': '2026-05-11T11:00:00Z'
    });
  });
});

function chat(overrides: Partial<ChatSummary>): ChatSummary {
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
    updatedAt: null,
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
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => {
      values.set(key, value);
    }
  };
}
