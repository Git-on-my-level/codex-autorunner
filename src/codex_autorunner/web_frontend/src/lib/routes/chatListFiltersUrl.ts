import type {
  ChatFacetCategory,
  ChatFacetScopeKind,
  ChatFacetTransport
} from '$lib/api/readModelContracts';
import { CHAT_FILTER_ORDER, type ChatStatusFilter } from '$lib/viewModels/pmaChat';

export type ChatListFilters = {
  status: ChatStatusFilter;
  category: ChatFacetCategory | null;
  transport: ChatFacetTransport | null;
  scopeKind: ChatFacetScopeKind | null;
  search: string;
};

/** Query keys owned by the chats list filter bar (not detail/draft hooks). */
export const CHAT_LIST_FILTER_QUERY_KEYS = ['filter', 'search', 'category', 'transport', 'scope_kind'] as const;

const STATUS_VALUES = new Set<ChatStatusFilter>(CHAT_FILTER_ORDER);
const CATEGORY_VALUES = new Set<ChatFacetCategory>(['regular', 'ticket_run', 'automation', 'system']);
const TRANSPORT_VALUES = new Set<ChatFacetTransport>(['pma', 'discord', 'telegram', 'notification']);
const SCOPE_KIND_VALUES = new Set<ChatFacetScopeKind>(['hub', 'repo', 'worktree', 'filesystem']);

export const DEFAULT_CHAT_LIST_FILTERS: ChatListFilters = {
  status: 'all',
  category: null,
  transport: null,
  scopeKind: null,
  search: ''
};

function parseStatusFilter(raw: string | null): ChatStatusFilter {
  if (raw && STATUS_VALUES.has(raw as ChatStatusFilter)) return raw as ChatStatusFilter;
  return 'all';
}

function parseCategoryFilter(raw: string | null): ChatFacetCategory | null {
  if (raw && CATEGORY_VALUES.has(raw as ChatFacetCategory)) return raw as ChatFacetCategory;
  return null;
}

function parseTransportFilter(raw: string | null): ChatFacetTransport | null {
  if (raw && TRANSPORT_VALUES.has(raw as ChatFacetTransport)) return raw as ChatFacetTransport;
  return null;
}

function parseScopeKindFilter(raw: string | null): ChatFacetScopeKind | null {
  if (raw && SCOPE_KIND_VALUES.has(raw as ChatFacetScopeKind)) return raw as ChatFacetScopeKind;
  return null;
}

export function parseChatListFiltersFromSearchParams(
  searchParams: URLSearchParams
): ChatListFilters {
  return {
    status: parseStatusFilter(searchParams.get('filter')),
    category: parseCategoryFilter(searchParams.get('category')),
    transport: parseTransportFilter(searchParams.get('transport')),
    scopeKind: parseScopeKindFilter(searchParams.get('scope_kind')),
    search: searchParams.get('search') ?? ''
  };
}

export function applyChatListFiltersToSearchParams(
  params: URLSearchParams,
  filters: ChatListFilters
): URLSearchParams {
  for (const key of CHAT_LIST_FILTER_QUERY_KEYS) params.delete(key);
  if (filters.status !== 'all') params.set('filter', filters.status);
  const query = filters.search.trim();
  if (query) params.set('search', query);
  if (filters.category) params.set('category', filters.category);
  if (filters.transport) params.set('transport', filters.transport);
  if (filters.scopeKind) params.set('scope_kind', filters.scopeKind);
  return params;
}

export function serializeChatListFilterQuery(filters: ChatListFilters): string {
  return applyChatListFiltersToSearchParams(new URLSearchParams(), filters).toString();
}

export function chatListFiltersEqual(a: ChatListFilters, b: ChatListFilters): boolean {
  return (
    a.status === b.status &&
    a.category === b.category &&
    a.transport === b.transport &&
    a.scopeKind === b.scopeKind &&
    a.search === b.search
  );
}

export function buildChatsListHref(
  filters: ChatListFilters,
  options: {
    chatId?: string | null;
    preserveParams?: URLSearchParams;
    withHref?: (path: string) => string;
  } = {}
): string {
  const params = applyChatListFiltersToSearchParams(
    new URLSearchParams(options.preserveParams ?? undefined),
    filters
  );
  const chatId = options.chatId?.trim();
  const path = chatId ? `/chats/${encodeURIComponent(chatId)}` : '/chats';
  const query = params.toString();
  const raw = `${path}${query ? `?${query}` : ''}`;
  return options.withHref ? options.withHref(raw) : raw;
}

export function toggleChatStatusFilter(
  current: ChatStatusFilter,
  next: ChatStatusFilter
): ChatStatusFilter {
  return current === next ? 'all' : next;
}

export function toggleChatFacetFilter<T extends string>(current: T | null, next: T): T | null {
  return current === next ? null : next;
}
