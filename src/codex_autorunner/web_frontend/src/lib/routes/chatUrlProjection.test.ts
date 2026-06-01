import { describe, expect, it, vi } from 'vitest';
import {
  pushChatDetailProjection,
  replaceChatDetailProjection,
  replaceChatListFiltersProjection
} from './chatUrlProjection';
import type { ChatListFilters } from './chatListFiltersUrl';

const filters: ChatListFilters = {
  status: 'waiting',
  category: 'ticket_run',
  transport: null,
  scopeKind: null,
  search: 'queue'
};

describe('chatUrlProjection', () => {
  it('projects chat list filters with replaceState instead of navigation', () => {
    const history = mockHistory();

    const target = replaceChatListFiltersProjection(filters, {
      chatId: 'chat-1',
      url: new URL('http://localhost/chats/chat-1?tab=activity&draft=old'),
      history,
      withHref: (path) => `/base${path}`
    });

    expect(target).toBe('/base/chats/chat-1?tab=activity&filter=waiting&search=queue&category=ticket_run');
    expect(history.replaceState).toHaveBeenCalledWith({ preserved: true }, '', target);
    expect(history.pushState).not.toHaveBeenCalled();
  });

  it('strips transient detail hooks when replacing or pushing committed chat detail URLs', () => {
    const history = mockHistory();
    const url = new URL('http://localhost/chats?chat=old&detail=chat%3Aold&draft=x&new=agent&kind=agent&tab=activity');

    expect(replaceChatDetailProjection('chat-2', { url, history, withHref: (path) => path })).toBe(
      '/chats/chat-2?tab=activity'
    );
    expect(pushChatDetailProjection('chat-3', { url, history, withHref: (path) => path })).toBe(
      '/chats/chat-3?tab=activity'
    );
    expect(history.replaceState).toHaveBeenCalledWith({ preserved: true }, '', '/chats/chat-2?tab=activity');
    expect(history.pushState).toHaveBeenCalledWith({ preserved: true }, '', '/chats/chat-3?tab=activity');
  });
});

function mockHistory(): Pick<History, 'pushState' | 'replaceState' | 'state'> {
  return {
    state: { preserved: true },
    pushState: vi.fn(),
    replaceState: vi.fn()
  };
}
