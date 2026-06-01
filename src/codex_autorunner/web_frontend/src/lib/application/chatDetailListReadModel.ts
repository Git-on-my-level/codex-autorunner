import type { ChatIndexWindowRequest } from '$lib/data';
import type {
  ChatFacetCategory,
  ChatFacetScopeKind,
  ChatFacetTransport,
  ChatFacetCounts,
  ChatIndexCounters
} from '$lib/api/readModelContracts';
import type { ChatDraftRecord } from './chatDraftStore';
import type { ChatSummary } from '$lib/viewModels/domain';
import {
  adjustedUnreadFilterCount,
  chatCategoryLabel,
  chatFacets,
  chatTransportLabel,
  filterChats,
  summarizeVisibleLocalPlaceholderStatusCounts,
  CHAT_EXTERNAL_TRANSPORT_FILTERS,
  type ChatStatusFilter
} from '$lib/viewModels/chat';
import type { ChatLastSeenMap } from '$lib/viewModels/unread';

export type ChatDetailListFilters = {
  status: ChatStatusFilter;
  category: ChatFacetCategory | null;
  transport: ChatFacetTransport | null;
  scopeKind: ChatFacetScopeKind | null;
  search: string;
};

export type ChatDetailFilterOption<T extends string> = {
  key: T;
  label: string;
  count: number;
};

export function buildDraftChatSummary(
  record: ChatDraftRecord,
  known: ChatSummary | null
): ChatSummary {
  const raw = known?.raw ?? {};
  return {
    id: record.chatId,
    title: known?.title ?? record.chatId,
    lifecycleStatus: known?.lifecycleStatus ?? 'active',
    status: known?.status ?? 'idle',
    agentId: known?.agentId ?? null,
    chatKind: known?.chatKind ?? null,
    agentProfile: known?.agentProfile ?? null,
    model: known?.model ?? null,
    runtime: known?.runtime ?? null,
    runtimeSource: known?.runtimeSource ?? null,
    modelSource: known?.modelSource ?? null,
    reasoning: known?.reasoning ?? null,
    reasoningSource: known?.reasoningSource ?? null,
    repoId: known?.repoId ?? null,
    worktreeId: known?.worktreeId ?? null,
    ticketId: known?.ticketId ?? null,
    ticketPath: known?.ticketPath ?? null,
    runId: known?.runId ?? null,
    unreadCount: known?.unreadCount ?? 0,
    flowType: known?.flowType ?? null,
    isTicketFlow: known?.isTicketFlow ?? false,
    ticketDone: known?.ticketDone ?? null,
    ticketStatus: known?.ticketStatus ?? null,
    progressPercent: known?.progressPercent ?? null,
    updatedAt: record.updatedAt,
    raw: {
      ...raw,
      has_local_draft: true,
      local_draft_updated_at: record.updatedAt
    }
  };
}

export function filterDraftChatsForChatList(
  source: ChatSummary[],
  filters: ChatDetailListFilters,
  lastSeenMap: ChatLastSeenMap
): ChatSummary[] {
  return filterChats(source, 'drafts', filters.search, lastSeenMap).filter((chat) => {
    const facets = chatFacets(chat);
    if (filters.category && facets?.category !== filters.category) return false;
    if (filters.transport && !facets?.transports.includes(filters.transport)) return false;
    if (filters.scopeKind && facets?.scopeKind !== filters.scopeKind) return false;
    return true;
  });
}

export function buildChatIndexRequest(filters: ChatDetailListFilters): ChatIndexWindowRequest {
  return {
    filter: backendChatIndexFilter(filters.status),
    query: filters.search.trim() || null,
    facets: {
      categories: filters.category ? [filters.category] : [],
      transports: filters.transport ? [filters.transport] : [],
      scopeKinds: filters.scopeKind ? [filters.scopeKind] : []
    },
    groupBy: filters.category === 'ticket_run' ? 'ticket_run' : null,
    limit: 50
  };
}

export function buildChatTicketRunGroupRequest(
  filters: ChatDetailListFilters
): ChatIndexWindowRequest {
  return {
    filter: backendChatIndexFilter(filters.status),
    query: filters.search.trim() || null,
    facets: {
      categories: ['ticket_run'],
      transports: filters.transport ? [filters.transport] : [],
      scopeKinds: filters.scopeKind ? [filters.scopeKind] : []
    },
    groupBy: 'ticket_run',
    limit: 50
  };
}

export function backendChatIndexFilter(
  filter: ChatStatusFilter
): ChatIndexWindowRequest['filter'] {
  return filter === 'drafts' ? 'all' : filter;
}

export function buildChatStatusFilterCounts(input: {
  counters: ChatIndexCounters;
  statusFilter: ChatStatusFilter;
  knownChats: ChatSummary[];
  lastSeenMap: ChatLastSeenMap;
  persistedFacetChats: ChatSummary[];
  committedChatPlaceholders: ChatSummary[];
  localChatPlaceholderCount: number;
  filteredDraftChatsLength: number;
  draftChatsLength: number;
}): Record<ChatStatusFilter, number> {
  const localStatusCounts = summarizeVisibleLocalPlaceholderStatusCounts(
    input.persistedFacetChats,
    input.committedChatPlaceholders
  );
  return {
    all: input.counters.total + input.localChatPlaceholderCount,
    active: input.counters.running + localStatusCounts.active,
    waiting: input.counters.waiting + localStatusCounts.waiting,
    unread: adjustedUnreadFilterCount(input.counters.unread, input.knownChats, input.lastSeenMap),
    drafts: input.statusFilter === 'drafts'
      ? input.filteredDraftChatsLength
      : input.draftChatsLength,
    archived: input.counters.archived
  };
}

export function buildChatCategoryFilterOptions(input: {
  counts: ChatFacetCounts['category'];
  ticketRunGroupCount: number;
  selectedCategory: ChatFacetCategory | null;
}): ChatDetailFilterOption<ChatFacetCategory>[] {
  const order: ChatDetailFilterOption<ChatFacetCategory>[] = [
    { key: 'regular', label: chatCategoryLabel('regular'), count: input.counts.regular ?? 0 },
    { key: 'ticket_run', label: chatCategoryLabel('ticket_run'), count: input.ticketRunGroupCount },
    { key: 'automation', label: chatCategoryLabel('automation'), count: input.counts.automation ?? 0 },
    { key: 'system', label: chatCategoryLabel('system'), count: input.counts.system ?? 0 }
  ];
  return order.filter((item) => item.count > 0 || input.selectedCategory === item.key);
}

export function buildChatTransportFilterOptions(input: {
  counts: ChatFacetCounts['transport'];
  selectedTransport: ChatFacetTransport | null;
}): ChatDetailFilterOption<ChatFacetTransport>[] {
  return [...CHAT_EXTERNAL_TRANSPORT_FILTERS]
    .map((transport) => ({
      key: transport,
      label: chatTransportLabel(transport),
      count: input.counts[transport] ?? 0
    }))
    .filter((item) => item.count > 0 || input.selectedTransport === item.key);
}

export function buildChatScopeKindFilterOptions(input: {
  counts: ChatFacetCounts['scopeKind'];
  selectedScopeKind: ChatFacetScopeKind | null;
  label: (scopeKind: ChatFacetScopeKind) => string;
}): ChatDetailFilterOption<ChatFacetScopeKind>[] {
  const order: ChatFacetScopeKind[] = ['hub', 'repo', 'worktree', 'filesystem'];
  return order
    .map((scopeKind) => ({
      key: scopeKind,
      label: input.label(scopeKind),
      count: input.counts[scopeKind] ?? 0
    }))
    .filter((item) => item.count > 0 || input.selectedScopeKind === item.key);
}
