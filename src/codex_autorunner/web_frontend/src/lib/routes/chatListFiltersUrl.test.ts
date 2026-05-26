import { describe, expect, it } from 'vitest';
import {
  applyChatListFiltersToSearchParams,
  buildChatsListHref,
  chatListFiltersEqual,
  DEFAULT_CHAT_LIST_FILTERS,
  parseChatListFiltersFromSearchParams,
  serializeChatListFilterQuery,
  toggleChatFacetFilter,
  toggleChatStatusFilter
} from './chatListFiltersUrl';

describe('chatListFiltersUrl', () => {
  it('round-trips list filters through search params', () => {
    const filters = {
      status: 'waiting' as const,
      category: 'ticket_run' as const,
      transport: 'discord' as const,
      scopeKind: 'worktree' as const,
      search: 'billing'
    };
    const params = applyChatListFiltersToSearchParams(new URLSearchParams('draft=1'), filters);
    expect(params.get('draft')).toBe('1');
    expect(params.get('filter')).toBe('waiting');
    expect(params.get('category')).toBe('ticket_run');
    expect(params.get('transport')).toBe('discord');
    expect(params.get('scope_kind')).toBe('worktree');
    expect(params.get('search')).toBe('billing');
    expect(parseChatListFiltersFromSearchParams(params)).toEqual(filters);
  });

  it('omits default filter params and ignores unknown values', () => {
    const params = new URLSearchParams(
      'filter=nope&category=unknown&transport=carrier&scope_kind=cloud&search='
    );
    expect(parseChatListFiltersFromSearchParams(params)).toEqual(DEFAULT_CHAT_LIST_FILTERS);
    expect(serializeChatListFilterQuery(DEFAULT_CHAT_LIST_FILTERS)).toBe('');
  });

  it('builds chat detail paths while preserving unrelated query params', () => {
    const href = buildChatsListHref(
      { status: 'active', category: null, transport: null, scopeKind: null, search: 'ops' },
      {
        chatId: 'chat-1',
        preserveParams: new URLSearchParams('tab=activity'),
        withHref: (path) => `/base${path}`
      }
    );
    expect(href).toBe('/base/chats/chat-1?tab=activity&filter=active&search=ops');
  });

  it('strips stale chat detail query hooks when building canonical chat urls', () => {
    const href = buildChatsListHref(DEFAULT_CHAT_LIST_FILTERS, {
      chatId: 'chat-1',
      preserveParams: new URLSearchParams('chat=old&detail=chat%3Aold&draft=x&new=agent&kind=agent&tab=activity')
    });

    expect(href).toBe('/chats/chat-1?tab=activity');
  });

  it('toggles status and facet filters like the list chips', () => {
    expect(toggleChatStatusFilter('active', 'active')).toBe('all');
    expect(toggleChatStatusFilter('all', 'waiting')).toBe('waiting');
    expect(toggleChatFacetFilter('discord', 'discord')).toBeNull();
    expect(toggleChatFacetFilter(null, 'discord')).toBe('discord');
  });

  it('compares filter snapshots for url sync', () => {
    expect(
      chatListFiltersEqual(
        { status: 'all', category: null, transport: null, scopeKind: null, search: '' },
        { ...DEFAULT_CHAT_LIST_FILTERS }
      )
    ).toBe(true);
    expect(
      chatListFiltersEqual(
        { status: 'all', category: null, transport: null, scopeKind: null, search: '' },
        { status: 'all', category: null, transport: null, scopeKind: null, search: 'x' }
      )
    ).toBe(false);
  });
});
