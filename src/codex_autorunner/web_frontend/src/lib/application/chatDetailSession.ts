import type { ApiError } from '$lib/api/client';
import type { ReadModelLoaderResult } from '$lib/data';
import type { PmaChatSummary } from '$lib/viewModels/domain';
import {
  committedDraftChatPlaceholder,
  isLocalChatPlaceholder,
  isPmaChatArchived,
  pmaChatBindingKey,
  type ChatListEntry,
  type ChatRunGroup
} from '$lib/viewModels/pmaChat';
import {
  markAllChatsRead,
  markChatRead,
  saveLastSeenMap,
  type ChatLastSeenMap
} from '$lib/viewModels/unread';

export const PINNED_CHATS_STORAGE_KEY = 'car.webHub.pinnedChats.v1';

export type ChatDetailMode = 'list' | 'detail';
export type PinnedChatMap = Record<string, true>;

export type BrowserStorageAdapter = Pick<Storage, 'getItem' | 'setItem'>;

export type ChatDetailSessionState = {
  activeChatId: string | null;
  detailMode: ChatDetailMode;
  localDraftChat: PmaChatSummary | null;
  committedDraftChat: PmaChatSummary | null;
  pendingCommittedDetailUrlChatId: string | null;
  loadingActive: boolean;
  activeError: ApiError | null;
};

export type ChatDetailSelectionCommand = {
  state: ChatDetailSessionState;
  refresh: { chatId: string; quiet: boolean } | null;
  syncUrl: boolean;
  markRead: boolean;
  syncSelectors: boolean;
  closeStream: boolean;
};

export type ChatDetailActivationInput = {
  detailId: string | null;
  chats: PmaChatSummary[];
  hasCachedDetail: (chatId: string) => boolean;
  activeDetailLoadResult: (chatId: string) => ReadModelLoaderResult | null;
};

export type ChatDetailRequestedRowsInput = {
  loadedChats: PmaChatSummary[];
  requestedChatId: string | null;
  hasCachedDetail: (chatId: string) => boolean;
};

export type ChatDetailReadMarkerStore = {
  save: (markers: ChatLastSeenMap) => void;
};

export const browserReadMarkerStore: ChatDetailReadMarkerStore = {
  save: saveLastSeenMap
};

export function initialChatDetailSessionState(): ChatDetailSessionState {
  return {
    activeChatId: null,
    detailMode: 'list',
    localDraftChat: null,
    committedDraftChat: null,
    pendingCommittedDetailUrlChatId: null,
    loadingActive: false,
    activeError: null
  };
}

export function isLocalDraftChatId(chatId: string | null): boolean {
  return Boolean(chatId && chatId.startsWith('draft:pma:'));
}

export function chatSummaryForSessionId(
  chatId: string | null,
  chats: PmaChatSummary[],
  localDraftChat: PmaChatSummary | null
): PmaChatSummary | null {
  if (!chatId) return null;
  if (localDraftChat?.id === chatId) return localDraftChat;
  return chats.find((chat) => chat.id === chatId) ?? null;
}

export function requestedChatDetailFromUrl(
  routeChatId: string | null | undefined,
  searchParams: Pick<URLSearchParams, 'get'>
): string | null {
  if (routeChatId) return routeChatId;
  const detail = searchParams.get('detail');
  if (detail?.startsWith('chat:')) return detail.slice('chat:'.length);
  return searchParams.get('chat');
}

export function selectChatDetail(
  state: ChatDetailSessionState,
  chatId: string,
  options: { cached: boolean; loaderOwnsInitialDetail?: boolean; syncUrl?: boolean; activeError?: ApiError | null } = { cached: false }
): ChatDetailSelectionCommand {
  const loaderOwnsInitialDetail = Boolean(options.loaderOwnsInitialDetail);
  const activeError = options.activeError ?? null;
  return {
    state: {
      ...state,
      localDraftChat: isLocalDraftChatId(chatId) ? state.localDraftChat : null,
      committedDraftChat: state.committedDraftChat?.id === chatId ? state.committedDraftChat : null,
      pendingCommittedDetailUrlChatId: null,
      activeChatId: chatId,
      detailMode: 'detail',
      loadingActive: activeError ? false : !(options.cached || loaderOwnsInitialDetail),
      activeError
    },
    refresh: activeError ? null : { chatId, quiet: options.cached || loaderOwnsInitialDetail },
    syncUrl: options.syncUrl ?? false,
    markRead: true,
    syncSelectors: true,
    closeStream: false
  };
}

export function activateChatDetailFromUrl(
  state: ChatDetailSessionState,
  input: ChatDetailActivationInput
): ChatDetailSelectionCommand {
  const { detailId } = input;
  if (!detailId) {
    if (isLocalDraftChatId(state.activeChatId)) return noDetailCommand(state);
    if (state.pendingCommittedDetailUrlChatId && state.activeChatId === state.pendingCommittedDetailUrlChatId) {
      return noDetailCommand(state);
    }
    if (state.activeChatId === null) return noDetailCommand(state);
    return {
      state: {
        ...state,
        activeChatId: null,
        detailMode: 'list',
        loadingActive: false,
        activeError: null
      },
      refresh: null,
      syncUrl: false,
      markRead: false,
      syncSelectors: false,
      closeStream: true
    };
  }
  if (detailId === state.activeChatId) return noDetailCommand(state);
  if (isLocalDraftChatId(state.activeChatId)) return noDetailCommand(state);
  if (state.localDraftChat && detailId === state.localDraftChat.id) {
    return selectChatDetail(state, detailId, { cached: input.hasCachedDetail(detailId), syncUrl: false });
  }
  if (!input.chats.some((chat) => chat.id === detailId)) {
    const loaderResult = input.activeDetailLoadResult(detailId);
    const activeError = loaderResult?.status === 'error' ? loaderResult.error : null;
    return {
      state: {
        ...state,
        committedDraftChat: state.committedDraftChat?.id === detailId ? state.committedDraftChat : null,
        pendingCommittedDetailUrlChatId: null,
        activeChatId: detailId,
        detailMode: 'detail',
        loadingActive: !activeError,
        activeError
      },
      refresh: activeError ? null : { chatId: detailId, quiet: false },
      syncUrl: false,
      markRead: false,
      syncSelectors: false,
      closeStream: true
    };
  }
  const loaderResult = input.activeDetailLoadResult(detailId);
  return selectChatDetail(state, detailId, {
    cached: input.hasCachedDetail(detailId),
    loaderOwnsInitialDetail: Boolean(loaderResult && loaderResult.status !== 'cold'),
    syncUrl: false
  });
}

export function activateRequestedChatFromRows(
  state: ChatDetailSessionState,
  input: ChatDetailRequestedRowsInput
): ChatDetailSelectionCommand {
  if (isLocalDraftChatId(state.activeChatId)) return noDetailCommand(state);
  if (input.requestedChatId && !input.loadedChats.some((chat) => chat.id === input.requestedChatId)) {
    return noDetailCommand(state);
  }
  const selectedChatId = chooseActiveChatId(input.loadedChats, state.activeChatId, input.requestedChatId);
  if (!selectedChatId || state.activeChatId === selectedChatId) return noDetailCommand(state);
  const command = selectChatDetail(state, selectedChatId, {
    cached: input.hasCachedDetail(selectedChatId),
    syncUrl: false
  });
  return {
    ...command,
    state: {
      ...command.state,
      detailMode: 'detail'
    }
  };
}

export function archivedFilterForSelectedChat(
  chats: PmaChatSummary[],
  selectedChatId: string | null
): 'archived' | null {
  const selected = chats.find((chat) => chat.id === selectedChatId) ?? null;
  return selected && isPmaChatArchived(selected) ? 'archived' : null;
}

export function replacementForArchivedActiveChat(
  previousChats: PmaChatSummary[],
  nextChats: PmaChatSummary[],
  activeChatId: string | null
): string | null {
  if (!activeChatId) return null;
  const previousActive = previousChats.find((chat) => chat.id === activeChatId) ?? null;
  const previousBinding = pmaChatBindingKey(previousActive);
  if (!previousBinding) return null;
  const nextActive = nextChats.find((chat) => chat.id === activeChatId) ?? null;
  if (nextActive && !isPmaChatArchived(nextActive)) return null;
  const replacement = nextChats.find(
    (chat) => chat.id !== activeChatId && pmaChatBindingKey(chat) === previousBinding && !isPmaChatArchived(chat)
  );
  return replacement?.id ?? null;
}

export function clearCommittedDraftPlaceholderIfPersisted(
  state: ChatDetailSessionState,
  persistedChats: PmaChatSummary[]
): ChatDetailSessionState {
  if (!state.committedDraftChat) return state;
  return persistedChats.some((chat) => chat.id === state.committedDraftChat?.id)
    ? { ...state, committedDraftChat: null }
    : state;
}

export function startLocalDraftChat(
  state: ChatDetailSessionState,
  draftChat: PmaChatSummary
): ChatDetailSessionState {
  return {
    ...state,
    localDraftChat: draftChat,
    committedDraftChat: null,
    pendingCommittedDetailUrlChatId: null,
    activeChatId: draftChat.id,
    detailMode: 'detail',
    loadingActive: false,
    activeError: null
  };
}

export function commitLocalDraftChat(
  state: ChatDetailSessionState,
  draftChat: PmaChatSummary | null,
  committedChatId: string,
  updatedAt?: string
): ChatDetailSessionState {
  return {
    ...state,
    localDraftChat: null,
    committedDraftChat: draftChat ? committedDraftChatPlaceholder(draftChat, committedChatId, updatedAt) : null,
    pendingCommittedDetailUrlChatId: committedChatId,
    activeChatId: committedChatId,
    detailMode: 'detail',
    loadingActive: false,
    activeError: null
  };
}

export function markSessionChatRead(
  markers: ChatLastSeenMap,
  chatId: string | null,
  chats: PmaChatSummary[],
  localDraftChat: PmaChatSummary | null,
  store: ChatDetailReadMarkerStore = browserReadMarkerStore,
  fallbackStamp = new Date().toISOString()
): ChatLastSeenMap {
  if (!chatId) return markers;
  const chat = chatSummaryForSessionId(chatId, chats, localDraftChat);
  const next = markChatRead(markers, chatId, chat?.updatedAt ?? fallbackStamp);
  if (next !== markers) store.save(next);
  return next;
}

export function markVisibleChatsRead(
  markers: ChatLastSeenMap,
  chats: PmaChatSummary[],
  store: ChatDetailReadMarkerStore = browserReadMarkerStore
): ChatLastSeenMap {
  const next = markAllChatsRead(markers, chats.filter((chat) => !isPmaChatArchived(chat)));
  if (next !== markers) store.save(next);
  return next;
}

export function markChatGroupRead(
  markers: ChatLastSeenMap,
  chats: PmaChatSummary[],
  store: ChatDetailReadMarkerStore = browserReadMarkerStore,
  fallbackStamp = new Date().toISOString()
): ChatLastSeenMap {
  let next = markers;
  for (const chat of chats) {
    const stamp = chat.updatedAt ?? fallbackStamp;
    if (next[chat.id] && next[chat.id] >= stamp) continue;
    next = markChatRead(next, chat.id, stamp);
  }
  if (next !== markers) store.save(next);
  return next;
}

export function markActiveSummaryRead(
  markers: ChatLastSeenMap,
  chat: PmaChatSummary | null,
  store: ChatDetailReadMarkerStore = browserReadMarkerStore
): ChatLastSeenMap {
  if (!chat?.updatedAt) return markers;
  const next = markChatRead(markers, chat.id, chat.updatedAt);
  if (next !== markers) store.save(next);
  return next;
}

export function loadPinnedChats(storage: BrowserStorageAdapter | null = browserLocalStorage()): PinnedChatMap {
  if (!storage) return {};
  try {
    const raw = storage.getItem(PINNED_CHATS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return {};
    return Object.fromEntries(parsed.filter((id): id is string => typeof id === 'string' && id.trim().length > 0).map((id) => [id, true]));
  } catch {
    return {};
  }
}

export function savePinnedChats(next: PinnedChatMap, storage: BrowserStorageAdapter | null = browserLocalStorage()): void {
  if (!storage) return;
  try {
    storage.setItem(PINNED_CHATS_STORAGE_KEY, JSON.stringify(Object.keys(next).sort()));
  } catch {
    // Private mode / quota.
  }
}

export function togglePinnedChatId(pinned: PinnedChatMap, chatId: string): PinnedChatMap {
  const next = { ...pinned };
  if (next[chatId]) delete next[chatId];
  else next[chatId] = true;
  return next;
}

export function chatListVirtualKey(entry: ChatListEntry, pinned: PinnedChatMap): string {
  if (entry.kind === 'group') {
    const pinOrder = entry.group.chats
      .map((chat) => `${chat.id}:${pinned[chat.id] ? 1 : 0}`)
      .join('|');
    return `group:${entry.group.key}:${pinOrder}`;
  }
  return `chat:${entry.chat.id}:${pinned[entry.chat.id] ? 1 : 0}`;
}

export function pinAwareChatRowKey(chat: PmaChatSummary, pinned: PinnedChatMap): string {
  return `${chat.id}:${pinned[chat.id] ? 1 : 0}`;
}

export function sortEntriesForPinnedChats(entries: ChatListEntry[], pinned: PinnedChatMap): ChatListEntry[] {
  const decorated = entries.map((entry) => {
    if (entry.kind === 'chat') {
      return {
        entry,
        pinned: pinned[entry.chat.id] === true,
        placeholder: isLocalChatPlaceholder(entry.chat),
        sort: entry.chat.updatedAt ?? '',
        id: entry.chat.id
      };
    }
    const chats = [...entry.group.chats].sort((left, right) => {
      const pinnedDiff = Number(pinned[right.id] === true) - Number(pinned[left.id] === true);
      if (pinnedDiff !== 0) return pinnedDiff;
      return (right.updatedAt ?? '').localeCompare(left.updatedAt ?? '');
    });
    return {
      entry: { kind: 'group' as const, group: { ...entry.group, chats } satisfies ChatRunGroup },
      pinned: chats.some((chat) => pinned[chat.id] === true),
      placeholder: false,
      sort: entry.group.updatedAt ?? '',
      id: entry.group.key
    };
  });
  decorated.sort((left, right) => {
    const placeholderDiff = Number(right.placeholder) - Number(left.placeholder);
    if (placeholderDiff !== 0) return placeholderDiff;
    const pinnedDiff = Number(right.pinned) - Number(left.pinned);
    if (pinnedDiff !== 0) return pinnedDiff;
    const timeDiff = right.sort.localeCompare(left.sort);
    if (timeDiff !== 0) return timeDiff;
    return left.id.localeCompare(right.id);
  });
  return decorated.map((item) => item.entry);
}

function noDetailCommand(state: ChatDetailSessionState): ChatDetailSelectionCommand {
  return {
    state,
    refresh: null,
    syncUrl: false,
    markRead: false,
    syncSelectors: false,
    closeStream: false
  };
}

function chooseActiveChatId(
  chats: PmaChatSummary[],
  currentChatId: string | null,
  requestedChatId: string | null
): string | null {
  if (requestedChatId && chats.some((chat) => chat.id === requestedChatId)) return requestedChatId;
  if (currentChatId && chats.some((chat) => chat.id === currentChatId)) return currentChatId;
  return chats[0]?.id ?? null;
}

function browserLocalStorage(): BrowserStorageAdapter | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage;
}
