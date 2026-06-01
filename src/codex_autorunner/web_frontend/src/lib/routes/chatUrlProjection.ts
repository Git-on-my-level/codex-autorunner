import { withRuntimeBasePath as defaultHref } from '$lib/runtime/basePath';
import { chatRoute } from '$lib/viewModels/routes';
import {
  buildChatsListHref,
  type ChatListFilters
} from './chatListFiltersUrl';

export type ChatUrlProjectionRuntime = {
  url?: URL;
  history?: Pick<History, 'pushState' | 'replaceState' | 'state'>;
  withHref?: (path: string) => string;
};

const CHAT_DETAIL_QUERY_KEYS = ['chat', 'detail', 'draft', 'new', 'kind'] as const;

export function replaceChatListFiltersProjection(
  filters: ChatListFilters,
  options: ChatUrlProjectionRuntime & { chatId?: string | null } = {}
): string {
  const target = buildChatsListHref(filters, {
    chatId: options.chatId ?? null,
    preserveParams: currentProjectionUrl(options).searchParams,
    withHref: options.withHref ?? defaultHref
  });
  replaceProjectionUrl(target, options);
  return target;
}

export function replaceChatDetailProjection(
  detailId: string,
  options: ChatUrlProjectionRuntime = {}
): string {
  const target = buildChatDetailProjectionTarget(detailId, options);
  replaceProjectionUrl(target, options);
  return target;
}

export function pushChatDetailProjection(
  detailId: string,
  options: ChatUrlProjectionRuntime = {}
): string {
  const target = buildChatDetailProjectionTarget(detailId, options);
  const history = options.history ?? browserHistory();
  history?.pushState(history.state, '', target);
  return target;
}

function buildChatDetailProjectionTarget(detailId: string, options: ChatUrlProjectionRuntime): string {
  const params = new URLSearchParams(currentProjectionUrl(options).searchParams);
  for (const key of CHAT_DETAIL_QUERY_KEYS) params.delete(key);
  const raw = chatRoute(detailId, { searchParams: params });
  return (options.withHref ?? defaultHref)(raw);
}

function replaceProjectionUrl(target: string, options: ChatUrlProjectionRuntime): void {
  const history = options.history ?? browserHistory();
  history?.replaceState(history.state, '', target);
}

function currentProjectionUrl(options: ChatUrlProjectionRuntime): URL {
  if (options.url) return options.url;
  if (typeof window !== 'undefined') return new URL(window.location.href);
  return new URL('http://localhost/chats');
}

function browserHistory(): ChatUrlProjectionRuntime['history'] {
  return typeof history !== 'undefined' ? history : undefined;
}
