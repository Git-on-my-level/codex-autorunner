import type {
  ChatFacetCategory,
  ChatFacetCounts,
  ChatFacetScopeKind,
  ChatFacetTransport
} from '$lib/api/readModelContracts';
import type { ChatListFilters } from '$lib/routes/chatListFiltersUrl';
import type { ChatStatusFilter } from '$lib/viewModels/pmaChat';

export type ChatFilterSummaryChip = {
  id: 'status' | 'category' | 'transport' | 'scopeKind' | 'search';
  label: string;
};

export function selectChatFacetCountsForWindow(
  hasWindow: boolean,
  windowFacetCounts: ChatFacetCounts,
  globalFacetCounts: ChatFacetCounts
): ChatFacetCounts {
  return hasWindow ? windowFacetCounts : globalFacetCounts;
}

/** Hide status pills with zero matches unless selected; suppress live buckets on archived view. */
export function shouldShowChatStatusFilterPill(
  key: ChatStatusFilter,
  counts: Record<ChatStatusFilter, number>,
  selected: ChatStatusFilter
): boolean {
  if (key === 'all') return false;
  if (counts[key] > 0) return true;
  if (selected === key) return true;
  if (selected === 'archived' && (key === 'waiting' || key === 'active' || key === 'unread')) {
    return false;
  }
  return false;
}

export function buildChatFilterSummaryChips(
  filters: ChatListFilters,
  labels: {
    status: (key: ChatStatusFilter) => string;
    category: (key: ChatFacetCategory) => string;
    transport: (key: ChatFacetTransport) => string;
    scopeKind: (key: ChatFacetScopeKind) => string;
  }
): ChatFilterSummaryChip[] {
  const chips: ChatFilterSummaryChip[] = [];
  if (filters.status !== 'all') {
    chips.push({ id: 'status', label: labels.status(filters.status) });
  }
  if (filters.category) {
    chips.push({ id: 'category', label: labels.category(filters.category) });
  }
  if (filters.transport) {
    chips.push({ id: 'transport', label: labels.transport(filters.transport) });
  }
  if (filters.scopeKind) {
    chips.push({ id: 'scopeKind', label: labels.scopeKind(filters.scopeKind) });
  }
  const query = filters.search.trim();
  if (query) {
    chips.push({ id: 'search', label: query });
  }
  return chips;
}

export function formatChatListResultSummary(count: number | null, loading = false): string {
  if (loading || count === null) return '… chats';
  if (count === 1) return '1 chat';
  return `${count} chats`;
}
