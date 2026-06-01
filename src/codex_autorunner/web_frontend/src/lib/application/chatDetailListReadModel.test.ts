import { describe, expect, it } from 'vitest';
import type { ChatFacetCounts, ChatIndexCounters } from '$lib/api/readModelContracts';
import type { ChatDraftRecord } from './chatDraftStore';
import type { ChatSummary } from '$lib/viewModels/domain';
import {
  backendChatIndexFilter,
  buildChatCategoryFilterOptions,
  buildChatIndexRequest,
  buildChatScopeKindFilterOptions,
  buildChatStatusFilterCounts,
  buildChatTicketRunGroupRequest,
  buildChatTransportFilterOptions,
  buildDraftChatSummary,
  filterDraftChatsForChatList
} from './chatDetailListReadModel';

describe('chat detail list read model', () => {
  it('builds screen-shaped chat index requests from page filters', () => {
    expect(buildChatIndexRequest({
      status: 'drafts',
      category: 'ticket_run',
      transport: 'discord',
      scopeKind: 'repo',
      search: '  bug  '
    })).toEqual({
      filter: 'all',
      query: 'bug',
      facets: {
        categories: ['ticket_run'],
        transports: ['discord'],
        scopeKinds: ['repo']
      },
      groupBy: 'ticket_run',
      limit: 50
    });

    expect(buildChatTicketRunGroupRequest({
      status: 'waiting',
      category: null,
      transport: null,
      scopeKind: null,
      search: ''
    })).toMatchObject({
      filter: 'waiting',
      query: null,
      facets: { categories: ['ticket_run'], transports: [], scopeKinds: [] },
      groupBy: 'ticket_run'
    });
    expect(backendChatIndexFilter('active')).toBe('active');
  });

  it('projects local draft records without making the Svelte page own row shape', () => {
    const summary = buildDraftChatSummary(draftRecord('chat-1'), {
      ...chatSummary('chat-1'),
      title: 'Existing chat',
      status: 'running',
      raw: { source: 'backend' }
    });

    expect(summary).toMatchObject({
      id: 'chat-1',
      title: 'Existing chat',
      status: 'running',
      updatedAt: '2026-05-20T12:00:00.000Z',
      raw: {
        source: 'backend',
        has_local_draft: true,
        local_draft_updated_at: '2026-05-20T12:00:00.000Z'
      }
    });
  });

  it('filters draft rows through the same facet semantics as persisted chats', () => {
    const repoDraft = {
      ...chatSummary('repo-chat'),
      repoId: 'repo-1',
      raw: {
        has_local_draft: true,
        facets: {
          category: 'regular',
          turnKinds: ['message'],
          originKinds: ['surface'],
          transports: ['discord'],
          scopeKind: 'repo'
        }
      }
    };
    const hubDraft = {
      ...chatSummary('hub-chat'),
      raw: {
        has_local_draft: true,
        facets: {
          category: 'regular',
          turnKinds: ['message'],
          originKinds: ['surface'],
          transports: ['web'],
          scopeKind: 'hub'
        }
      }
    };

    const filtered = filterDraftChatsForChatList([repoDraft, hubDraft], {
      status: 'drafts',
      category: 'regular',
      transport: 'discord',
      scopeKind: 'repo',
      search: 'repo'
    }, {});

    expect(filtered.map((chat) => chat.id)).toEqual(['repo-chat']);
  });

  it('combines backend counters, local placeholders, unread adjustment, and draft counts', () => {
    const counts = buildChatStatusFilterCounts({
      counters: counters({ total: 3, running: 1, waiting: 1, unread: 2, archived: 4 }),
      statusFilter: 'all',
      knownChats: [chatSummary('seen'), chatSummary('unseen')],
      lastSeenMap: {
        seen: '2026-05-20T12:01:00.000Z'
      },
      persistedFacetChats: [],
      committedChatPlaceholders: [{ ...chatSummary('local'), status: 'running' }],
      localChatPlaceholderCount: 1,
      filteredDraftChatsLength: 2,
      draftChatsLength: 5
    });

    expect(counts).toMatchObject({
      all: 4,
      active: 2,
      waiting: 1,
      drafts: 5,
      archived: 4
    });
  });

  it('keeps selected zero-count facet options visible', () => {
    const facetCounts = emptyFacetCounts();

    expect(buildChatCategoryFilterOptions({
      counts: facetCounts.category,
      ticketRunGroupCount: 0,
      selectedCategory: 'automation'
    }).map((item) => item.key)).toEqual(['automation']);
    expect(buildChatTransportFilterOptions({
      counts: facetCounts.transport,
      selectedTransport: 'telegram'
    }).map((item) => item.key)).toEqual(['telegram']);
    expect(buildChatScopeKindFilterOptions({
      counts: facetCounts.scopeKind,
      selectedScopeKind: 'worktree',
      label: (value) => value.toUpperCase()
    })).toEqual([{ key: 'worktree', label: 'WORKTREE', count: 0 }]);
  });
});

function draftRecord(chatId: string): ChatDraftRecord {
  return {
    chatId,
    text: 'draft text',
    updatedAt: '2026-05-20T12:00:00.000Z'
  };
}

function chatSummary(id: string): ChatSummary {
  return {
    id,
    title: id,
    lifecycleStatus: 'active',
    status: 'idle',
    agentId: null,
    chatKind: null,
    agentProfile: null,
    model: null,
    runtime: null,
    runtimeSource: null,
    modelSource: null,
    reasoning: null,
    reasoningSource: null,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    ticketPath: null,
    runId: null,
    unreadCount: 0,
    flowType: null,
    isTicketFlow: false,
    ticketDone: null,
    ticketStatus: null,
    progressPercent: null,
    updatedAt: '2026-05-20T11:00:00.000Z',
    raw: {}
  };
}

function counters(overrides: Partial<ChatIndexCounters> = {}): ChatIndexCounters {
  return {
    total: 0,
    waiting: 0,
    running: 0,
    unread: 0,
    archived: 0,
    ...overrides
  };
}

function emptyFacetCounts(): ChatFacetCounts {
  return {
    category: {},
    turnKind: {},
    originKind: {},
    transport: {},
    scopeKind: {},
    agentKind: {}
  };
}
