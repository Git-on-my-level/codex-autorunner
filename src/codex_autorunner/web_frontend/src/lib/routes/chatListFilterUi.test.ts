import { describe, expect, it } from 'vitest';
import {
  buildChatFilterSummaryChips,
  formatChatListResultSummary,
  selectChatFacetCountsForWindow,
  shouldShowChatStatusFilterPill
} from './chatListFilterUi';
import { DEFAULT_CHAT_LIST_FILTERS } from './chatListFiltersUrl';

const windowFacetCounts = {
  category: { regular: 8, system: 2 },
  turnKind: {},
  originKind: {},
  transport: { pma: 10 },
  scopeKind: { worktree: 10 },
  agentKind: {}
};

const globalFacetCounts = {
  category: { regular: 25, system: 59 },
  turnKind: {},
  originKind: {},
  transport: { pma: 86 },
  scopeKind: { worktree: 10, filesystem: 64 },
  agentKind: {}
};

describe('chatListFilterUi', () => {
  it('prefers window facet counts when the filtered index window is loaded', () => {
    expect(
      selectChatFacetCountsForWindow(true, windowFacetCounts, globalFacetCounts).transport.pma
    ).toBe(10);
    expect(
      selectChatFacetCountsForWindow(false, windowFacetCounts, globalFacetCounts).transport.pma
    ).toBe(86);
  });

  it('hides zero status pills except the selected one, and suppresses live buckets on archived', () => {
    const counts = { all: 10, waiting: 0, active: 0, unread: 0, drafts: 0, archived: 10 };
    expect(shouldShowChatStatusFilterPill('waiting', counts, 'archived')).toBe(false);
    expect(shouldShowChatStatusFilterPill('archived', counts, 'archived')).toBe(true);
    expect(shouldShowChatStatusFilterPill('waiting', counts, 'all')).toBe(false);
    expect(shouldShowChatStatusFilterPill('waiting', { ...counts, waiting: 2 }, 'all')).toBe(true);
    expect(shouldShowChatStatusFilterPill('waiting', counts, 'waiting')).toBe(true);
  });

  it('builds removable summary chips for active filters', () => {
    const chips = buildChatFilterSummaryChips(
      {
        ...DEFAULT_CHAT_LIST_FILTERS,
        status: 'archived',
        transport: 'pma',
        scopeKind: 'worktree',
        search: 'ticket'
      },
      {
        status: (key) => (key === 'archived' ? 'Archived' : key),
        category: (key) => key,
        transport: (key) => key.toUpperCase(),
        scopeKind: (key) => key
      }
    );
    expect(chips).toEqual([
      { id: 'status', label: 'Archived' },
      { id: 'transport', label: 'PMA' },
      { id: 'scopeKind', label: 'worktree' },
      { id: 'search', label: 'ticket' }
    ]);
  });

  it('formats list summary counts', () => {
    expect(formatChatListResultSummary(null, true)).toBe('… chats');
    expect(formatChatListResultSummary(1)).toBe('1 chat');
    expect(formatChatListResultSummary(10)).toBe('10 chats');
  });
});
