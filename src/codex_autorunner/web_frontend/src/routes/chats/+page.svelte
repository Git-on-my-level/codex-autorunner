<script lang="ts">
  import { afterNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount, tick } from 'svelte';
  import MasterDetail from '$lib/components/MasterDetail.svelte';
  import ChatTranscriptCards from '$lib/components/ChatTranscriptCards.svelte';
  import VirtualList from '$lib/components/VirtualList.svelte';
  import FilterRow from '$lib/components/FilterRow.svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import ChatThreadPreMessagePickers from '$lib/components/ChatThreadPreMessagePickers.svelte';
  import VoiceComposerButton from '$lib/components/VoiceComposerButton.svelte';
  import ContentSkeleton from '$lib/components/ContentSkeleton.svelte';
  import OverflowMenu from '$lib/components/OverflowMenu.svelte';
  import type { OverflowMenuItem } from '$lib/components/OverflowMenu';
  import { confirmDialog } from '$lib/components/confirmDialog';
  import {
    webApi,
    type ApiError,
    type ChatFileBoxScope,
    type FileBoxName,
    type JsonRecord,
    type PmaQueuedTurn
  } from '$lib/api/client';
  import {
    invalidateReadModelTags,
    readModelEntityStore,
    readModelEntityTags,
    type ChatIndexWindowRequest,
    selectChatIndexWindowView,
    selectPmaArtifacts,
    selectPmaChats,
    selectPmaProgress,
    selectPmaQueue,
    selectChatTranscript,
    selectTicketRunGroups,
    selectReadMarkers
  } from '$lib/data';
  import { chatIndexSession } from '$lib/data/chatIndexSession';
  import type {
    ChatFacetCategory,
    ChatFacetScopeKind,
    ChatFacetTransport
  } from '$lib/api/readModelContracts';
  import {
    buildChatDetailDisplayReadModel,
    isOptimisticQueuedTurn,
    progressWithLiveElapsed,
    type ChatTranscriptListItem
  } from '$lib/application/pmaChatArchitecture';
  import { createChatSendController } from '$lib/application/chatSendController';
  import {
    loadChatDraftRecords,
    saveChatDraftRecords,
    setChatDraftText,
    sortedChatDraftRecords,
    type ChatDraftRecord,
    type ChatDraftRecordMap
  } from '$lib/application/chatDraftStore';
  import { connectionStore } from '$lib/runtime/connectionStore.svelte';
  import {
    createChatDetailLiveProjection,
    type ChatDetailLiveProjectionState
  } from '$lib/application/chatDetailLiveProjection';
  import {
    chatListVirtualKey as chatListVirtualKeyForPins,
    chatSummaryForSessionId,
    initialChatDetailSessionState,
    markChatGroupRead,
    markSessionChatRead,
    markVisibleChatsRead,
    pinAwareChatRowKey as pinAwareChatRowKeyForPins,
    savePinnedChats,
    sortEntriesForPinnedChats,
    startLocalDraftChat,
    togglePinnedChatId,
    type ChatDetailSessionState
  } from '$lib/application/chatDetailSession';
  import {
    createChatDetailPageController,
    type ChatDetailPageSupportData
  } from '$lib/application/chatDetailPageController';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    buildChatsListHref,
    chatListFiltersEqual,
    DEFAULT_CHAT_LIST_FILTERS,
    parseChatListFiltersFromSearchParams,
    toggleChatFacetFilter,
    toggleChatStatusFilter,
    type ChatListFilters
  } from '$lib/routes/chatListFiltersUrl';
  import {
    buildChatFilterSummaryChips,
    formatChatListResultSummary,
    selectChatFacetCountsForWindow,
    shouldShowChatStatusFilterPill,
    type ChatFilterSummaryChip
  } from '$lib/routes/chatListFilterUi';
  import {
    repoContextspaceRoute,
    repoRoute,
    repoTicketRoute,
    worktreeContextspaceRoute,
    worktreeRoute,
    worktreeTicketRoute,
    chatRoute
  } from '$lib/viewModels/routes';
  import type {
    PmaChatSummary,
    PmaRunProgress,
    ArtifactDelivery,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    adjustedUnreadFilterCount,
    buildSemanticChatListEntries,
    chatRunGroupSummaryParts,
    buildPmaChatScopeOptions,
    buildPmaLiveActivity,
    buildManagedThreadMessagePayload,
    countSemanticTicketRunGroups,
    filterChatEntries,
    filterPmaChats,
    formatBytes,
    formatRelativeTime,
    isPmaChatArchived,
    localPmaChatScopeOption,
    mergeChatFacetSourceChats,
    mergeLocalChatPlaceholders,
    CHAT_FILTER_ORDER,
    CHAT_EXTERNAL_TRANSPORT_FILTERS,
    CHAT_TICKET_RUNS_FILTER,
    pmaChatKind,
    pmaChatKindLabel,
    pmaChatHeaderScopeLine,
    pmaChatBadgeViews,
    pmaChatFacets,
    chatCategoryLabel,
    chatTransportLabel,
    pmaChatScopeTagView,
    progressPercent,
    visibleLocalChatPlaceholders as selectVisibleLocalChatPlaceholders,
    removePendingAttachment,
    statusLabel,
    summarizeVisibleLocalPlaceholderStatusCounts,
    type PendingAttachment,
    type ChatTranscriptCard,
    type ChatStatusFilter,
    type ChatListEntry,
    type ChatRunGroup,
    type PmaChatScopeOption,
    type PmaChatScopeSource
  } from '$lib/viewModels/pmaChat';
  import {
    loadLastSeenMap,
    isChatUnread,
    type ChatLastSeenMap
  } from '$lib/viewModels/unread';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import { surfaceRefFromThreadRaw } from '$lib/viewModels/thread';
  import {
    agentProfileEntriesForRecord,
    agentCanListModels,
    agentDisplayForChat,
    agentId,
    agentLabel,
    agentRecordForId,
    modelExists,
    modelLabel,
    pickerReasoningOptions,
    resolveAgentModelSelection,
    resolvePmaChatSelectorsForActiveChat,
    stringField
  } from '$lib/viewModels/modelPickers';
  import { getLastModelForAgent, persistLastModelForAgent } from '$lib/viewModels/lastModelByAgent';
  import {
    getLastNewChatPreference,
    persistLastNewChatPreference
  } from '$lib/viewModels/lastNewChatPreferences';
  import {
    buildSlashCommandSuggestions,
    parseSlashCommand,
    WEB_SLASH_COMMANDS,
    type SlashCommandSpec,
    type SlashCommandSuggestion
  } from '$lib/viewModels/slashCommands';
  import type { ChatRouteLoadData } from './+page';

  const COMPACT_SUMMARY_PROMPT =
    'Summarize the conversation so far into a concise context block I can paste into a new thread. Include goals, constraints, decisions, and current state.';
  let readModelState = $state(readModelEntityStore.snapshot());
  // Unsent new chats are page-local only: `localDraftChat` drives the composer until
  // the first send creates the managed thread. They are intentionally omitted from
  // the sidebar index (`chats`) so leaving the flow does not surface a draft row.
  let localDraftChat = $state<PmaChatSummary | null>(null);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let scopeOptions = $state<PmaChatScopeOption[]>(buildPmaChatScopeOptions([], []));
  let pendingAttachments = $state<PendingAttachment[]>([]);
  let configuredDefaultAgentId = $state<string | undefined>(undefined);
  let configuredDefaultProfile = $state('');
  let linkDialogOpen = $state(false);
  let fileDrawerOpen = $state(false);
  let linkDraft = $state('');
  let artifactDeliveries = $state<ArtifactDelivery[]>([]);
  let loadingArtifactDeliveries = $state(false);
  let artifactDeliveryError = $state<ApiError | null>(null);
  let artifactDeliveryLoadKey = '';
  let deletingFileKeys = $state<Set<string>>(new Set());
  let deletingFileBox = $state<FileBoxName | null>(null);
  let selectedAgent = $state('codex');
  let selectedModel = $state('');
  let selectedReasoning = $state('');
  let selectedProfile = $state('');
  let selectedScopeId = $state('local');
  let selectedScopeSource = $state<PmaChatScopeSource>('default_hub');
  let newChatKind = $state<'pma' | 'agent'>('pma');
  /** True when the scope picker should be locked. Set when entering chats via a
   *  non-local `?new=` deep-link from a repo or worktree page — the caller has
   *  declared which scope this chat belongs to. */
  let scopeLocked = $state(false);
  function readChatListFiltersFromRoute(): ChatListFilters {
    try {
      return parseChatListFiltersFromSearchParams(page.url.searchParams);
    } catch {
      return DEFAULT_CHAT_LIST_FILTERS;
    }
  }

  let statusFilter = $state<ChatStatusFilter>(DEFAULT_CHAT_LIST_FILTERS.status);
  let categoryFilter = $state<ChatFacetCategory | null>(DEFAULT_CHAT_LIST_FILTERS.category);
  let transportFilter = $state<ChatFacetTransport | null>(DEFAULT_CHAT_LIST_FILTERS.transport);
  let scopeKindFilter = $state<ChatFacetScopeKind | null>(DEFAULT_CHAT_LIST_FILTERS.scopeKind);
  let detailMode = $state<'list' | 'detail'>('list');
  let search = $state(DEFAULT_CHAT_LIST_FILTERS.search);
  let chatDraftRecords = $state<ChatDraftRecordMap>({});
  let pendingAttachmentsByChat = $state<Record<string, PendingAttachment[]>>({});
  let draftHydratedChatId: string | null | undefined = undefined;
  let pendingInitialDraftText: string | null = null;
  const chatListFilters = $derived<ChatListFilters>({
    status: statusFilter,
    category: categoryFilter,
    transport: transportFilter,
    scopeKind: scopeKindFilter,
    search
  });
  const lastSeenMap = $derived<ChatLastSeenMap>(selectReadMarkers(readModelState) as ChatLastSeenMap);
  const currentChatIndexRequest = $derived<ChatIndexWindowRequest>(chatIndexRequestForCurrentFilters());
  const ticketRunGroupRequest = $derived<ChatIndexWindowRequest>(chatTicketRunGroupRequestForFilters());
  const persistedChats = $derived<PmaChatSummary[]>(selectPmaChats(readModelState, currentChatIndexRequest));
  const facetPersistedChats = $derived<PmaChatSummary[]>(selectPmaChats(readModelState, { filter: 'all', limit: 50 }));
  const backendTicketRunGroups = $derived(selectTicketRunGroups(readModelState, ticketRunGroupRequest));
  const currentTicketRunGroups = $derived(
    categoryFilter === 'ticket_run'
      ? selectTicketRunGroups(readModelState, currentChatIndexRequest)
      : backendTicketRunGroups
  );
  const committedChatPlaceholders = $derived<PmaChatSummary[]>([]);
  const visibleLocalChatPlaceholders = $derived<PmaChatSummary[]>(
    selectVisibleLocalChatPlaceholders(persistedChats, committedChatPlaceholders)
  );
  const chats = $derived<PmaChatSummary[]>(
    mergeLocalChatPlaceholders(persistedChats, committedChatPlaceholders)
  );
  const persistedFacetChats = $derived<PmaChatSummary[]>(
    mergeChatFacetSourceChats(facetPersistedChats, persistedChats)
  );
  const facetChats = $derived<PmaChatSummary[]>(
    mergeChatFacetSourceChats(facetPersistedChats, persistedChats, committedChatPlaceholders)
  );
  const draftChats = $derived<PmaChatSummary[]>(
    sortedChatDraftRecords(chatDraftRecords).map(draftRecordChatSummary)
  );
  const filteredDraftChats = $derived<PmaChatSummary[]>(
    filterDraftChatsForCurrentFilters(draftChats)
  );
  const chatListSourceChats = $derived<PmaChatSummary[]>(
    statusFilter === 'drafts' ? filteredDraftChats : categoryFilter === 'ticket_run' ? facetChats : chats
  );
  const activeChatId = $derived<string | null>(readModelState.activeChatId);

  function chatSummaryForId(chatId: string | null): PmaChatSummary | null {
    return chatSummaryForSessionId(chatId, chats, localDraftChat)
      ?? (chatId ? selectPmaChats(readModelState).find((chat) => chat.id === chatId) ?? null : null);
  }

  function filterDraftChatsForCurrentFilters(source: PmaChatSummary[]): PmaChatSummary[] {
    return filterPmaChats(source, 'drafts', search, lastSeenMap).filter((chat) => {
      const facets = pmaChatFacets(chat);
      if (categoryFilter && facets?.category !== categoryFilter) return false;
      if (transportFilter && !facets?.transports.includes(transportFilter)) return false;
      if (scopeKindFilter && facets?.scopeKind !== scopeKindFilter) return false;
      return true;
    });
  }

  function draftRecordChatSummary(record: ChatDraftRecord): PmaChatSummary {
    const known =
      selectPmaChats(readModelState).find((chat) => chat.id === record.chatId) ??
      (localDraftChat?.id === record.chatId ? localDraftChat : null) ??
      record.chatSnapshot ??
      null;
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
  const transcriptCards = $derived<ChatTranscriptCard[]>(selectChatTranscript(readModelState, activeChatId));
  const progress = $derived<PmaRunProgress | null>(selectPmaProgress(readModelState, activeChatId));
  const artifacts = $derived<SurfaceArtifact[]>(selectPmaArtifacts(readModelState, activeChatId));
  const inboxArtifacts = $derived(artifacts.filter((artifact) => String(artifact.raw.box ?? '').toLowerCase() === 'inbox'));
  const outboxArtifacts = $derived(artifacts.filter((artifact) => String(artifact.raw.box ?? '').toLowerCase() === 'outbox'));
  const queuedTurns = $derived<PmaQueuedTurn[]>(selectPmaQueue(readModelState, activeChatId));
  let draft = $state('');
  let loadingChats = $state(true);
  let loadingMoreChats = $state(false);
  let refreshingActive = $state(false);
  let sending = $state(false);
  let creating = $state(false);
  let archiving = $state(false);
  let loadingModels = $state(false);
  /** Invalidates in-flight `listAgentModels` results when the user switches agents quickly. */
  let loadModelsSeq = 0;
  /**
   * `${chatId}|${agentId}` of the chat the selectors were last synced against,
   * or null if the last sync ran before the chat summary (and its bound agent)
   * was available. Lets the reactive re-sync fire exactly once per chat once
   * the summary arrives. See the syncSelectorsToActiveChat effect below.
   */
  let syncedSelectorKey: string | null = null;
  let chatError = $state<ApiError | null>(null);
  let activeError = $state<ApiError | null>(null);
  let composeError = $state<ApiError | null>(null);
  let streamState = $state<'idle' | 'connecting' | 'connected' | 'interrupted'>('idle');
  let streamError = $state<string | null>(null);
  const liveProjection = createChatDetailLiveProjection({
    api: webApi.pma,
    readModelStore: readModelEntityStore,
    getChatSummary: (chatId) => chatSummaryForId(chatId),
    onStateChange: writeLiveProjectionState
  });
  const pageController = createChatDetailPageController({
    readModelStore: readModelEntityStore,
    chatIndexSession,
    liveProjection,
    supportApi: {
      listFiles: () => webApi.filebox.listFiles({ kind: 'hub' }),
      listAgents: webApi.pma.listAgents,
      repoWorktreeTopology: webApi.readModels.repoWorktreeTopology,
      repoWorktreeRuntime: webApi.readModels.repoWorktreeRuntime
    },
    readSessionState: readChatDetailSessionState,
    writeSessionState: writeChatDetailSessionState,
    onReadModelState: (state) => {
      readModelState = state;
    },
    onLoadingChats: (value) => {
      loadingChats = value;
    },
    onChatError: (error) => {
      chatError = error;
    },
    onFilterArchived: () => {
      statusFilter = 'archived';
    },
    onClockTick: (nowMs) => {
      clockNowMs = nowMs;
    },
    onPinnedChatsLoaded: (pinned) => {
      pinnedChatIds = pinned;
    },
    onInitialDraft: (value) => {
      pendingInitialDraftText = value;
      setComposerDraft(value, null);
    },
    onCreateInitialDraft: () => {
      pendingInitialDraftCreate = true;
    },
    onSupportDataLoaded: applyInitialSupportingData,
    onSyncSelectors: syncSelectorsToActiveChat,
    onMarkRead: markActiveChatRead
  });
  let fileInput: HTMLInputElement | null = $state(null);
  let imageInput: HTMLInputElement | null = $state(null);
  let messageStack: HTMLDivElement | null = $state(null);
  let composerTextarea: HTMLTextAreaElement | null = $state(null);
  let voiceNotice = $state<string | null>(null);
  let commandNotice = $state<string | null>(null);
  let slashSelectedIndex = $state(0);
  let composerFocused = $state(false);
  let composerEditVersion = 0;
  let pendingInitialDraftCreate = false;
  let pendingPointerChatId: string | null = null;
  let removeDocumentChatPointerCapture: (() => void) | null = null;
  let transcriptAtBottom = $state(true);
  let transcriptApi: { scrollToBottom: (behavior?: ScrollBehavior) => void } | null = null;

  const COMPOSER_DEFAULT_MAX_PX = 360;
  const COMPOSER_MIN_MAX_PX = 120;
  const TRANSCRIPT_BOTTOM_THRESHOLD_PX = 80;
  let composerMaxPx = $state(COMPOSER_DEFAULT_MAX_PX);
  /** True when draft content wants at least composerMaxPx height (auto-grow hit the cap). */
  let showComposerResizeGrip = $state(false);
  function composerCeiling(): number {
    if (typeof window === 'undefined') return 720;
    return Math.max(COMPOSER_MIN_MAX_PX + 40, Math.round(window.innerHeight * 0.8));
  }
  function autosizeComposer(): void {
    const el = composerTextarea;
    if (!el) {
      showComposerResizeGrip = false;
      return;
    }
    el.style.height = 'auto';
    const cap = composerMaxPx;
    const natural = el.scrollHeight;
    showComposerResizeGrip = natural >= cap;
    const next = Math.min(natural, cap);
    el.style.height = `${next}px`;
    el.style.overflowY = natural > cap ? 'auto' : 'hidden';
  }
  function handleComposerResizeStart(event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startMax = composerMaxPx;
    const ceiling = composerCeiling();
    const onMove = (ev: PointerEvent) => {
      const delta = startY - ev.clientY; // dragging up = bigger composer
      const next = Math.min(ceiling, Math.max(COMPOSER_MIN_MAX_PX, startMax + delta));
      composerMaxPx = next;
      autosizeComposer();
    };
    const onUp = (ev: PointerEvent) => {
      target.releasePointerCapture(ev.pointerId);
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', onUp);
      target.removeEventListener('pointercancel', onUp);
    };
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', onUp);
    target.addEventListener('pointercancel', onUp);
  }
  function resetComposerHeight(): void {
    composerMaxPx = COMPOSER_DEFAULT_MAX_PX;
    autosizeComposer();
  }

  function persistDraftRecords(next: ChatDraftRecordMap): void {
    chatDraftRecords = next;
    saveChatDraftRecords(next);
  }

  function setComposerDraft(value: string, chatId: string | null = activeChatId): void {
    if (!chatId) {
      draft = value;
      return;
    }
    if (activeChatId === chatId) draft = value;
    persistDraftRecords(setChatDraftText(chatDraftRecords, chatId, value, chatSummaryForId(chatId)));
  }

  function setComposerAttachments(value: PendingAttachment[], chatId: string | null = activeChatId): void {
    if (!chatId) {
      pendingAttachments = value;
      return;
    }
    if (activeChatId === chatId) pendingAttachments = value;
    pendingAttachmentsByChat = value.length > 0
      ? { ...pendingAttachmentsByChat, [chatId]: value }
      : Object.fromEntries(Object.entries(pendingAttachmentsByChat).filter(([id]) => id !== chatId));
  }

  function syncComposerToActiveChat(): void {
    const chatId = activeChatId;
    if (draftHydratedChatId === chatId) return;
    draftHydratedChatId = chatId;
    const routeDraft = pendingInitialDraftText;
    const nextDraft = chatId ? (routeDraft ?? chatDraftRecords[chatId]?.text ?? '') : '';
    draft = nextDraft;
    pendingAttachments = chatId ? (pendingAttachmentsByChat[chatId] ?? []) : [];
    if (chatId && routeDraft !== null) {
      pendingInitialDraftText = null;
      persistDraftRecords(setChatDraftText(chatDraftRecords, chatId, routeDraft, chatSummaryForId(chatId)));
    }
    queueMicrotask(() => autosizeComposer());
  }

  function applyTranscript(text: string): void {
    if (!text) return;
    voiceNotice = null;
    const trimmed = text.trim();
    if (!trimmed) return;
    const sep = draft && !/\s$/.test(draft) ? ' ' : '';
    setComposerDraft(`${draft}${sep}${trimmed}`);
    markComposerEdited();
    queueMicrotask(() => {
      autosizeComposer();
      composerTextarea?.focus();
    });
  }

  function showVoiceNotice(message: string): void {
    voiceNotice = message;
    window.setTimeout(() => {
      if (voiceNotice === message) voiceNotice = null;
    }, 3500);
  }

  function showCommandNotice(message: string): void {
    commandNotice = message;
    window.setTimeout(() => {
      if (commandNotice === message) commandNotice = null;
    }, 3500);
  }

  function submitComposerFromDraft(): void {
    const slash = parseSlashCommand(draft);
    void (slash?.spec ? executeSlashCommand() : sendMessage());
  }

  $effect(() => {
    draft;
    composerMaxPx;
    autosizeComposer();
  });
  $effect(() => {
    slashSuggestions;
    slashSelectedIndex = Math.min(slashSelectedIndex, Math.max(slashSuggestions.length - 1, 0));
  });
  let clockNowMs = $state(Date.now());
  let lastScrolledChatId: string | null = null;
  let lastScrolledCardCount = 0;
  let lastScrolledEventCount = 0;
  // Sticky-bottom state: true while the user is parked at (or near) the
  // bottom of the transcript. Once they scroll up, we stop force-scrolling
  // on every content change; scrolling back near the bottom re-arms it.
  let followBottom = true;
  let messageStackResizeObserver: ResizeObserver | null = null;

  const activeChat = $derived(activeChatId ? chatSummaryForId(activeChatId) : null);
  const activeSurfaceDeliveries = $derived<ArtifactDelivery[]>(artifactDeliveriesForActiveSurface(activeChat, artifactDeliveries));
  const assistantSharedFiles = $derived<ArtifactDelivery[]>(activeSurfaceDeliveries.slice(0, 4));
  let expandedRunGroups = $state<Record<string, boolean>>({});
  let pinnedChatIds = $state<Record<string, true>>({});
  const chatListEntries = $derived(
    buildSemanticChatListEntries(chatListSourceChats, currentTicketRunGroups, {
      lastSeen: lastSeenMap,
      repoLabel: repoLabelForRepoId,
      worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null,
      groupRuns: true
    })
  );
  const selectedChatWindowView = $derived(selectChatIndexWindowView(readModelState, currentChatIndexRequest));
  const selectedChatWindow = $derived(selectedChatWindowView.window);
  const filteredEntries = $derived(
    sortEntriesForPinnedChats(
      filterChatEntries(chatListEntries, statusFilter === 'drafts' ? 'all' : statusFilter, '', lastSeenMap),
      pinnedChatIds
    )
  );
  const filterCounts = $derived(chatStatusFilterCounts());
  const contextualFacetCounts = $derived(
    selectChatFacetCountsForWindow(
      Boolean(selectedChatWindow),
      selectedChatWindowView.facetCounts,
      readModelState.chatFacetCounts
    )
  );
  const ticketRunGroupCount = $derived(countSemanticTicketRunGroups(backendTicketRunGroups, facetChats));
  const localChatPlaceholderCount = $derived(visibleLocalChatPlaceholders.filter((chat) => !isPmaChatArchived(chat)).length);
  const activeChatCount = $derived(readModelState.chatCounters.total + localChatPlaceholderCount);
  const hasUsableChatIndex = $derived(Boolean(readModelState.chatIndexCursor || readModelState.chatOrder.length > 0));
  const hasSelectedChatWindow = $derived(Boolean(selectedChatWindow));
  const chatListSummaryLoading = $derived(loadingChats && !hasSelectedChatWindow);
  const chatListSummaryCount = $derived(
    chatListSummaryLoading ? null : filterCounts[statusFilter]
  );
  const filterSummaryChips = $derived(
    buildChatFilterSummaryChips(chatListFilters, {
      status: filterChipLabel,
      category: chatCategoryLabel,
      transport: chatTransportLabel,
      scopeKind: facetChipLabel
    })
  );
  const chatListResultSummary = $derived(
    formatChatListResultSummary(chatListSummaryCount, chatListSummaryLoading)
  );
  const archiveFilterCount = $derived(filterCounts.archived);
  const showArchiveFilterToggle = $derived(statusFilter === 'archived' || archiveFilterCount > 0);
  const canLoadMoreChats = $derived(statusFilter !== 'drafts' && Boolean(selectedChatWindow?.window?.nextCursor));
  const initialChatIndexError = $derived(chatIndexLoadError());
  const visibleChatError = $derived(chatError ?? (!hasUsableChatIndex ? initialChatIndexError : null));
  const showChatListSkeleton = $derived(
    loadingChats && !hasSelectedChatWindow && !visibleChatError && !(statusFilter === 'drafts' && filteredDraftChats.length > 0)
  );

  function isGroupExpanded(group: ChatRunGroup): boolean {
    if (group.key in expandedRunGroups) return expandedRunGroups[group.key];
    // Default collapsed; expand only when the active chat lives inside this group.
    if (activeChatId && group.chats.some((chat) => chat.id === activeChatId)) return true;
    return false;
  }

  function toggleGroup(group: ChatRunGroup): void {
    expandedRunGroups = { ...expandedRunGroups, [group.key]: !isGroupExpanded(group) };
  }

  function toggleChatPinned(event: MouseEvent, chatId: string): void {
    event.preventDefault();
    event.stopPropagation();
    pendingPointerChatId = null;
    const next = togglePinnedChatId(pinnedChatIds, chatId);
    pinnedChatIds = next;
    savePinnedChats(next);
  }

  /** VirtualList keys must change when pin state or pin-driven order changes, or keyed {#each} reuses stale row DOM. */
  function chatListVirtualKey(entry: ChatListEntry): string {
    return chatListVirtualKeyForPins(entry, pinnedChatIds);
  }

  function pinAwareChatRowKey(chat: PmaChatSummary): string {
    return pinAwareChatRowKeyForPins(chat, pinnedChatIds);
  }

  function markGroupRead(group: ChatRunGroup): void {
    const next = markChatGroupRead(lastSeenMap, group.chats);
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-group:${group.key}:${Date.now()}`);
  }

  function groupBadgeClass(group: ChatRunGroup): string {
    return `chat-run-status-pill ${group.status}`;
  }

  function groupSummaryParts(group: ChatRunGroup): string[] {
    return chatRunGroupSummaryParts(group);
  }

  async function loadMoreChats(): Promise<void> {
    if (loadingMoreChats || !canLoadMoreChats) return;
    loadingMoreChats = true;
    try {
      await pageController.loadMoreIndex(currentChatIndexRequest);
      chatError = null;
    } catch (error) {
      chatError = {
        kind: 'network',
        status: 0,
        code: 'chat_index_load_more_failed',
        message: error instanceof Error ? error.message : 'Could not load more chats',
        details: error
      };
    } finally {
      loadingMoreChats = false;
    }
  }
  const displayedProgress = $derived(progressWithLiveElapsed(progress, clockNowMs));
  const liveActivity = $derived(buildPmaLiveActivity(displayedProgress));
  const chatDetailDisplay = $derived(
    buildChatDetailDisplayReadModel({
      transcriptCards,
      queuedTurns,
      displayedProgress,
      activeChat,
      assistantSharedFileCount: assistantSharedFiles.length,
      streamState,
      loadingActive: refreshingActive,
      activeError,
      draft,
      pendingAttachmentCount: pendingAttachments.length
    })
  );
  const activeCards = $derived<ChatTranscriptCard[]>(chatDetailDisplay.activeCards);
  const statusBar = $derived(chatDetailDisplay.statusBar);
  const selectedScope = $derived(scopeOptions.find((scope) => scope.id === selectedScopeId) ?? localPmaChatScopeOption());
  const selectedAgentRecord = $derived(agentRecordForId(agents, selectedAgent));
  const hermesProfileChoices = $derived(agentProfileEntriesForRecord(selectedAgentRecord));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const reasoningOptions = $derived(pickerReasoningOptions(models, selectedModel));
  const showAgentSelector = $derived(Boolean(activeChat && agents.length > 0));
  const showModelSelector = $derived(Boolean(activeChat && selectedAgentCanListModels && (loadingModels || models.length > 0)));
  const showEffortSelector = $derived(Boolean(showModelSelector && reasoningOptions.length > 0));
  const canStartCodingAgentChat = $derived(selectedScope.kind !== 'local');
  const activeChatKind = $derived(pmaChatKind(activeChat));
  const activeChatKindLabel = $derived(pmaChatKindLabel(activeChatKind));
  // Single source of truth for the chat's agent display name. Reads from the
  // picker, which is kept in sync with chat.agentId by syncSelectorsToActiveChat
  // and falls back to the user's configured default. This is the same value
  // the header config line uses, so the in-transcript assistant label, the
  // kind badge, and the header config line never disagree.
  const chatAgentDisplayLabel = $derived.by(() => {
    if (!activeChat) return 'Assistant';
    if (selectedAgentRecord) return agentLabel(selectedAgentRecord);
    const selectedTrim = selectedAgent?.trim();
    if (selectedTrim) return selectedTrim;
    return activeChatKindLabel;
  });
  const chatSendController = createChatSendController({
    api: webApi,
    readModelStore: readModelEntityStore,
    getActiveChatId: () => activeChatId,
    getActiveChat: () => activeChat,
    getDisplayedProgress: () => displayedProgress,
    getDraft: () => draft,
    setDraft: (value, chatId) => {
      setComposerDraft(value, chatId ?? activeChatId);
    },
    getPendingAttachments: () => pendingAttachments,
    setPendingAttachments: (value, chatId) => {
      setComposerAttachments(value, chatId ?? activeChatId);
    },
    getComposerEditVersion: () => composerEditVersion,
    getSelectedScope: () => selectedScope,
    getSelectedScopeSource: () => selectedScopeSource,
    getSelectedAgent: () => selectedAgent,
    getSelectedProfile: () => selectedProfile,
    getSelectedModel: () => selectedModel,
    getSelectedReasoning: () => selectedReasoning,
    getNewChatKind: () => newChatKind,
    canStartCodingAgentChat: () => canStartCodingAgentChat,
    newChatDisplayName,
    readSessionState: readChatDetailSessionState,
    writeSessionState: writeChatDetailSessionState,
    getLocalDraftChat: () => localDraftChat,
    invalidateChatMutation,
    refreshActive,
    setSending: (value) => {
      sending = value;
    },
    setComposeError: (error) => {
      composeError = error;
    },
    confirm: confirmDialog
  });
  const streamingMessageId = $derived(chatDetailDisplay.streamingMessageId);
  const transcriptListItems = $derived<ChatTranscriptListItem[]>(chatDetailDisplay.transcriptListItems);
  const srAnnouncement = $derived(chatDetailDisplay.srAnnouncement);
  const activeChatBadges = $derived(pmaChatBadgeViews(activeChat, { showPmaAgent: false }));
  const activeSharedFileCount = $derived(activeSurfaceDeliveries.length);
  const activeRepoIngress = $derived(repoIngressForChat(activeChat));
  const createChatLabel = $derived(creating ? 'Creating...' : '+ New');
  const headerScopeLine = $derived(pmaChatHeaderScopeLine(activeChat, repoLabelForRepoId));
  /** Omit connected “Live · …” — redundant with the turn-status pill on the scope row. */
  const showStreamHealthAside = $derived(chatDetailDisplay.showStreamHealthAside);
  const showStatusBar = $derived(chatDetailDisplay.showStatusBar);
  const chatHasActivity = $derived(chatDetailDisplay.chatHasActivity);
  const showStartPicker = $derived(chatDetailDisplay.showStartPicker);
  const hasRunnableDraft = $derived(chatDetailDisplay.hasRunnableDraft);
  const canInterruptWithDraft = $derived(chatDetailDisplay.canInterruptWithDraft);
  const composerWillQueue = $derived(chatDetailDisplay.composerWillQueue);
  const slashSuggestions = $derived<SlashCommandSuggestion[]>(
    buildSlashCommandSuggestions(draft, {
      hasActiveChat: Boolean(activeChat),
      hasScopedWorkspace: selectedScope.kind !== 'local',
      isRunning: displayedProgress?.status === 'running',
      queueDepth: chatDetailDisplay.queueDepthForCommands
    })
  );
  const showSlashCommandMenu = $derived(slashSuggestions.length > 0 && composerFocused);
  type ModelLoadMode = 'draft' | 'chat-bound';
  type ModelLoadOptions = {
    preferredModel?: string | null;
    mode?: ModelLoadMode;
  };

  function markComposerEdited(): void {
    composerEditVersion += 1;
  }

  function repoLabelForRepoId(repoId: string): string | null {
    const opt = scopeOptions.find((scope) => scope.kind === 'repo' && scope.resourceId === repoId);
    return opt?.label ?? null;
  }

  function chatRepoGlyphLabel(chat: PmaChatSummary): string {
    if (chat.repoId) {
      const opt = scopeOptions.find((scope) => scope.kind === 'repo' && scope.resourceId === chat.repoId);
      return opt?.label ?? chat.repoId;
    }
    if (chat.worktreeId) {
      const opt = worktreeScopeOption(chat.worktreeId);
      return opt?.label ?? chat.worktreeId;
    }
    return chat.title;
  }

  /** Label used with `repoAccent` / `repoInitials` for list scope coloring (repo, worktree, or hub basename). */
  function chatListScopeAccentLabel(chat: PmaChatSummary, scopeTags: ReturnType<typeof pmaChatScopeTagView>): string | null {
    if (chat.repoId || chat.worktreeId) return chatRepoGlyphLabel(chat);
    if (scopeTags.kindKey === 'hub') return scopeTags.detail;
    return null;
  }

  function filterChipLabel(key: ChatStatusFilter): string {
    if (key === 'drafts') return 'Drafts';
    return key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1);
  }

  function facetChipLabel(value: string): string {
    return value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ');
  }

  function chatIndexRequestForCurrentFilters(): ChatIndexWindowRequest {
    const facets = {
      categories: categoryFilter ? [categoryFilter] : [],
      transports: transportFilter ? [transportFilter] : [],
      scopeKinds: scopeKindFilter ? [scopeKindFilter] : []
    };
    return {
      filter: backendChatIndexFilter(statusFilter),
      query: search.trim() || null,
      facets,
      groupBy: categoryFilter === 'ticket_run' ? 'ticket_run' : null,
      limit: 50
    };
  }

  function chatTicketRunGroupRequestForFilters(): ChatIndexWindowRequest {
    return {
      filter: backendChatIndexFilter(statusFilter),
      query: search.trim() || null,
      facets: {
        categories: ['ticket_run'],
        transports: transportFilter ? [transportFilter] : [],
        scopeKinds: scopeKindFilter ? [scopeKindFilter] : []
      },
      groupBy: 'ticket_run',
      limit: 50
    };
  }

  function chatStatusFilterCounts(): Record<ChatStatusFilter, number> {
    const counters = selectedChatWindowView.window ? selectedChatWindowView.counters : readModelState.chatCounters;
    const knownChats = facetChats;
    const localStatusCounts = summarizeVisibleLocalPlaceholderStatusCounts(persistedFacetChats, committedChatPlaceholders);
    return {
      all: counters.total + localChatPlaceholderCount,
      active: counters.running + localStatusCounts.active,
      waiting: counters.waiting + localStatusCounts.waiting,
      unread: adjustedUnreadFilterCount(counters.unread, knownChats, lastSeenMap),
      drafts: statusFilter === 'drafts' ? filteredDraftChats.length : sortedChatDraftRecords(chatDraftRecords).length,
      archived: counters.archived
    };
  }

  function backendChatIndexFilter(filter: ChatStatusFilter): ChatIndexWindowRequest['filter'] {
    return filter === 'drafts' ? 'all' : filter;
  }

  const categoryFilterOptions = $derived(chatCategoryFilterOptions());
  const transportFilterOptions = $derived(chatTransportFilterOptions());
  const scopeKindFilterOptions = $derived(chatScopeKindFilterOptions());

  function clearFilterSummaryChip(chip: ChatFilterSummaryChip): void {
    if (chip.id === 'status') statusFilter = 'all';
    else if (chip.id === 'category') categoryFilter = null;
    else if (chip.id === 'transport') transportFilter = null;
    else if (chip.id === 'scopeKind') scopeKindFilter = null;
    else if (chip.id === 'search') search = '';
  }

  function chatCategoryFilterOptions(): { key: ChatFacetCategory; label: string; count: number }[] {
    const counts = contextualFacetCounts.category;
    const order: { key: ChatFacetCategory; label: string; count: number }[] = [
      { key: 'regular', label: chatCategoryLabel('regular'), count: counts.regular ?? 0 },
      { key: 'ticket_run', label: chatCategoryLabel('ticket_run'), count: ticketRunGroupCount },
      { key: 'automation', label: chatCategoryLabel('automation'), count: counts.automation ?? 0 },
      { key: 'system', label: chatCategoryLabel('system'), count: counts.system ?? 0 }
    ];
    return order.filter((item) => item.count > 0 || categoryFilter === item.key);
  }

  const hasNonStatusFilter = $derived(
    categoryFilter !== null || transportFilter !== null || scopeKindFilter !== null
  );
  const hasAnyFilter = $derived(hasNonStatusFilter || statusFilter !== 'all' || search.trim().length > 0);
  const hasAdvancedFilterOptions = $derived(
    categoryFilterOptions.length > 0 ||
      transportFilterOptions.length > 0 ||
      scopeKindFilterOptions.length > 0
  );
  const activeFacetFilterCount = $derived(
    (categoryFilter ? 1 : 0) + (transportFilter ? 1 : 0) + (scopeKindFilter ? 1 : 0)
  );
  let facetFiltersExpanded = $state(false);

  $effect(() => {
    syncFacetFiltersExpandedFromFilters(chatListFilters);
  });

  function syncFacetFiltersExpandedFromFilters(filters: ChatListFilters): void {
    if (filters.category || filters.transport || filters.scopeKind) {
      facetFiltersExpanded = true;
    }
  }

  function clearAllFilters(): void {
    statusFilter = 'all';
    categoryFilter = null;
    transportFilter = null;
    scopeKindFilter = null;
    search = '';
    facetFiltersExpanded = false;
  }

  function hydrateChatListFiltersFromUrl(): void {
    const fromUrl = readChatListFiltersFromRoute();
    if (chatListFiltersEqual(fromUrl, chatListFilters)) return;
    statusFilter = fromUrl.status;
    categoryFilter = fromUrl.category;
    transportFilter = fromUrl.transport;
    scopeKindFilter = fromUrl.scopeKind;
    search = fromUrl.search;
    syncFacetFiltersExpandedFromFilters(fromUrl);
  }

  function chatsHubHref(chatId: string | null = null): string {
    let preserveParams: URLSearchParams | undefined;
    try {
      preserveParams = page.url.searchParams;
    } catch {
      preserveParams = undefined;
    }
    return buildChatsListHref(chatListFilters, {
      chatId,
      preserveParams,
      withHref: href
    });
  }

  afterNavigate(() => {
    hydrateChatListFiltersFromUrl();
    pageController.setRoute(currentRouteSnapshot());
  });

  function chatTransportFilterOptions(): { key: ChatFacetTransport; label: string; count: number }[] {
    const counts = contextualFacetCounts.transport;
    return [...CHAT_EXTERNAL_TRANSPORT_FILTERS]
      .map((transport) => ({ key: transport, label: chatTransportLabel(transport), count: counts[transport] ?? 0 }))
      .filter((item) => item.count > 0 || transportFilter === item.key);
  }

  function chatScopeKindFilterOptions(): { key: ChatFacetScopeKind; label: string; count: number }[] {
    const counts = contextualFacetCounts.scopeKind;
    const order: ChatFacetScopeKind[] = ['hub', 'repo', 'worktree', 'filesystem'];
    return order
      .map((scopeKind) => ({ key: scopeKind, label: facetChipLabel(scopeKind), count: counts[scopeKind] ?? 0 }))
      .filter((item) => item.count > 0 || scopeKindFilter === item.key);
  }

  function composerRecipientLabel(chat: PmaChatSummary | null): string {
    if (!chat) return 'Chat';
    if (chat.repoId) {
      const repoLabel = repoLabelForRepoId(chat.repoId);
      return repoLabel ?? chat.repoId;
    }
    if (chat.worktreeId) {
      const opt = worktreeScopeOption(chat.worktreeId);
      return opt?.label ?? chat.worktreeId;
    }
    return 'Chat';
  }

  function chatHasLocalDraft(chat: PmaChatSummary): boolean {
    return Boolean(chatDraftRecords[chat.id]?.text.trim());
  }

  function runtimeRecordBoolean(record: Record<string, unknown> | null | undefined, ...keys: string[]): boolean {
    if (!record) return false;
    return keys.some((key) => record[key] === true);
  }

  function runtimeModelIsExplicitlyUnknown(chat: PmaChatSummary): boolean {
    if (chat.model) return false;
    return Boolean(
      chat.modelSource ||
        chat.runtimeSource ||
        runtimeRecordBoolean(chat.runtime, 'modelUnknown', 'model_unknown')
    );
  }

  function chatModelMetaLabel(chat: PmaChatSummary): string | null {
    const model = chat.model?.trim();
    if (model) return model;
    return runtimeModelIsExplicitlyUnknown(chat) ? 'model unknown' : null;
  }

  function chatModelMetaTitle(chat: PmaChatSummary): string | undefined {
    const parts = [
      chat.modelSource ? `model: ${chat.modelSource}` : null,
      chat.runtimeSource ? `runtime: ${chat.runtimeSource}` : null
    ].filter((part): part is string => Boolean(part));
    return parts.length > 0 ? parts.join(' · ') : undefined;
  }

  function activeChatRuntimeConfigLabel(chat: PmaChatSummary): string {
    const parts = [
      agentDisplayForChat(agents, chat) || chat.agentId || activeChatKindLabel,
      chatModelMetaLabel(chat),
      chat.reasoning ? `effort ${chat.reasoning}` : null
    ].filter((part): part is string => Boolean(part));
    return parts.join(' · ');
  }

  onMount(() => {
    chatDraftRecords = loadChatDraftRecords();
    hydrateChatListFiltersFromUrl();
    document.addEventListener('pointerdown', captureDocumentChatPointer, true);
    removeDocumentChatPointerCapture = () => {
      document.removeEventListener('pointerdown', captureDocumentChatPointer, true);
    };
    readModelEntityStore.setReadMarkers(loadLastSeenMap());
    pageController.mount({
      route: initialRouteSnapshot(),
      currentRequest: currentChatIndexRequest,
      ticketRunGroupRequest
    });
    const handlePopState = () => {
      pageController.setRoute(currentRouteSnapshot());
    };
    window.addEventListener('popstate', handlePopState);
    onDestroy(() => {
      window.removeEventListener('popstate', handlePopState);
    });
  });

  $effect(() => {
    pageController.setProgressStatus(progress?.status);
  });

  $effect(() => {
    activeChatId;
    syncComposerToActiveChat();
  });

  $effect(() => {
    pageController.clearCommittedDraftPlaceholder(persistedChats);
  });

  $effect(() => {
    if (newChatKind !== 'agent') return;
    if (canStartCodingAgentChat) return;
    const scoped = firstScopedScopeOption();
    if (scoped) {
      selectedScopeId = scoped.id;
      selectedScopeSource = 'picker_explicit';
      return;
    }
    newChatKind = 'pma';
  });

  $effect(() => {
    pageController.setIndexRequest(currentChatIndexRequest);
  });

  $effect(() => {
    chatIndexSession.setCompanionRequests([ticketRunGroupRequest]);
  });

  $effect(() => {
    try {
      const fromUrl = readChatListFiltersFromRoute();
      if (chatListFiltersEqual(chatListFilters, fromUrl)) return;
      void goto(chatsHubHref(activeChatId), {
        replaceState: true,
        keepFocus: true,
        noScroll: true
      });
    } catch {
      // No SvelteKit page context during SSR-only renders.
    }
  });

  $effect(() => {
    if (selectedReasoning && !reasoningOptions.includes(selectedReasoning)) selectedReasoning = '';
  });

  // Re-sync the agent/model/reasoning selectors once the active chat's summary
  // arrives. Opening a chat by URL before the chat index has loaded runs the
  // initial sync against a null summary, leaving the composer on the hub
  // default agent (codex). A Hermes chat would then send `model=gpt-5.4-mini`
  // (a Codex model) on every turn — the Hermes backend produces no assistant
  // output for a foreign model, so the agent silently stops responding.
  $effect(() => {
    const chat = activeChat;
    if (!chat || !chat.agentId) return;
    if (`${chat.id}|${chat.agentId}` === syncedSelectorKey) return;
    syncSelectorsToActiveChat();
  });

  $effect(() => {
    const chat = activeChat;
    if (!chat) {
      artifactDeliveries = [];
      artifactDeliveryError = null;
      artifactDeliveryLoadKey = '';
      return;
    }
    const key = `${chat.id}|${chat.repoId ?? ''}`;
    if (key === artifactDeliveryLoadKey) return;
    artifactDeliveryLoadKey = key;
    void refreshArtifactDeliveries(chat.repoId ?? null, key, { quiet: true });
    if (fileDrawerOpen) void refreshChatFileBox({ quiet: true });
  });

  onDestroy(() => {
    removeDocumentChatPointerCapture?.();
    pageController.destroy();
    connectionStore.reset();
  });

  $effect(() => {
    const cardCount = activeCards.length;
    const eventCount = progress?.events.length ?? 0;
    const chatChanged = activeChatId !== lastScrolledChatId;
    const cardCountChanged = cardCount !== lastScrolledCardCount;
    const eventCountChanged = eventCount !== lastScrolledEventCount;

    if (!activeChat || refreshingActive || (!chatChanged && !cardCountChanged && !eventCountChanged)) return;

    // Switching to a new chat re-arms sticky-bottom; otherwise let the
    // user's current followBottom state decide.
    if (chatChanged) followBottom = true;
    lastScrolledChatId = activeChatId;
    lastScrolledCardCount = cardCount;
    lastScrolledEventCount = eventCount;
    if (followBottom) void scrollMessagesToBottom();
  });

  function readChatDetailSessionState(): ChatDetailSessionState {
    return {
      ...initialChatDetailSessionState(),
      activeChatId,
      detailMode,
      localDraftChat,
      loadingActive: refreshingActive,
      activeError
    };
  }

  function writeChatDetailSessionState(state: ChatDetailSessionState): void {
    readModelEntityStore.setActiveChatId(state.activeChatId);
    if (
      detailMode === state.detailMode &&
      localDraftChat === state.localDraftChat &&
      refreshingActive === state.loadingActive &&
      activeError === state.activeError
    ) {
      return;
    }
    detailMode = state.detailMode;
    localDraftChat = state.localDraftChat;
    refreshingActive = state.loadingActive;
    activeError = state.activeError;
  }

  function writeLiveProjectionState(state: ChatDetailLiveProjectionState): void {
    refreshingActive = state.loadingActive;
    activeError = state.activeError;
    streamState = state.streamState;
    streamError = state.streamError;
    connectionStore.setStreamStatus(state.streamState);
  }

  function applyInitialSupportingData(data: ChatDetailPageSupportData): void {
    scopeOptions = data.scopeOptions;
    if (!scopeOptions.some((scope) => scope.id === selectedScopeId)) {
      selectedScopeId = 'local';
      selectedScopeSource = 'default_hub';
      scopeLocked = false;
    }
    if (!canStartCodingAgentChat) newChatKind = 'pma';
    agents = data.agents;
    const defaults = data.defaults;
    const defaultAgent =
      typeof defaults.agent === 'string' && defaults.agent.trim()
        ? defaults.agent.trim().toLowerCase()
        : data.defaultAgent;
    const defaultProfile =
      typeof defaults.profile === 'string' && defaults.profile.trim() ? defaults.profile.trim() : '';
    configuredDefaultAgentId =
      typeof defaultAgent === 'string' && defaultAgent.trim() ? defaultAgent.trim().toLowerCase() : undefined;
    configuredDefaultProfile = defaultProfile;
    if (!activeChat?.agentId) {
      const resolved = resolvePmaChatSelectorsForActiveChat(
        activeChat,
        agents,
        configuredDefaultAgentId,
        configuredDefaultProfile
      );
      if (resolved.mode === 'defaults') {
        selectedAgent = resolved.agentId;
        selectedProfile = resolved.agentProfile;
        selectedReasoning = resolved.reasoning;
      }
    }
    const activeChatAgentId = activeChat?.agentId;
    void loadModels(selectedAgent, {
      preferredModel: activeChatAgentId ? activeChat?.model : selectedModel,
      mode: activeChatAgentId ? 'chat-bound' : 'draft'
    });
    applyNewChatQueryParam();
    if (pendingInitialDraftCreate && !page.url.searchParams.get('new')) {
      pendingInitialDraftCreate = false;
      applyLastNewChatPreference();
      void createChat({ preserveSelectedScope: true });
    }
  }

  function applyNewChatQueryParam(): void {
    // Settings and repo/worktree pages use this URL hook to open an unsent, prefilled chat.
    const raw = page.url.searchParams.get('new');
    if (!raw) return;
    let decoded = raw;
    let appliedScope = false;
    try {
      decoded = decodeURIComponent(raw);
    } catch {
      decoded = raw;
    }
    if (decoded.startsWith('repo:')) {
      const sid = `repo:${decoded.slice('repo:'.length)}`;
      if (scopeOptions.some((scope) => scope.id === sid)) {
        selectedScopeId = sid;
        selectedScopeSource = 'route_explicit';
        appliedScope = true;
      }
    } else if (decoded.startsWith('worktree:')) {
      const sid = `worktree:${decoded.slice('worktree:'.length)}`;
      if (scopeOptions.some((scope) => scope.id === sid)) {
        selectedScopeId = sid;
        selectedScopeSource = 'route_explicit';
        appliedScope = true;
      }
    } else if (decoded === 'local' || decoded === 'hub') {
      selectedScopeId = 'local';
      selectedScopeSource = 'route_explicit';
      appliedScope = true;
    }
    const requestedKind = page.url.searchParams.get('kind');
    newChatKind = requestedKind === 'agent' && selectedScopeId !== 'local' ? 'agent' : 'pma';
    // Any non-local `?new=` deep-link is the caller saying "this chat belongs to
    // that scope" — pin the scope picker so the user can't accidentally rescope
    // a chat they entered from a repo/worktree page.
    scopeLocked = appliedScope && selectedScopeId !== 'local';
    const params = new URLSearchParams(page.url.searchParams);
    params.delete('new');
    params.delete('kind');
    const query = params.toString();
    void goto(href(`/chats${query ? `?${query}` : ''}`), { replaceState: true }).then(() => {
      if (appliedScope) {
        pendingInitialDraftCreate = false;
        persistCurrentNewChatPreference();
        void createChat({ preserveSelectedScope: true });
      }
    });
  }

  function applyLastNewChatPreference(): boolean {
    const preference = getLastNewChatPreference();
    if (!preference) return false;
    if (!scopeOptions.some((scope) => scope.id === preference.scopeId)) return false;
    selectedScopeId = preference.scopeId;
    selectedScopeSource = preference.scopeId === 'local' ? 'default_hub' : 'picker_explicit';
    newChatKind = preference.kind === 'agent' && preference.scopeId !== 'local' ? 'agent' : 'pma';
    return true;
  }

  async function loadModels(agentId: string, options: ModelLoadOptions = {}): Promise<void> {
    const preferredModel = options.preferredModel;
    const mode = options.mode ?? 'draft';
    const initialSelection = resolveAgentModelSelection({ agents, agentId });
    if (!initialSelection.canListModels) {
      loadModelsSeq += 1;
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    const seq = ++loadModelsSeq;
    loadingModels = true;
    models = [];
    const currentReasoning = selectedReasoning;
    selectedModel = '';
    selectedReasoning = '';

    const result = await webApi.pma.listAgentModels(agentId);
    if (seq !== loadModelsSeq) return;

    if (!result.ok) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    models = result.data;
    const selection = resolveAgentModelSelection({
      agents,
      agentId,
      catalog: result.data,
      preferredModel,
      rememberedModel: mode === 'draft' ? getLastModelForAgent(agentId) : null,
      currentReasoning,
      keepReasoning: true,
      allowEmptyModel: mode === 'chat-bound'
    });
    selectedModel = selection.model;
    selectedReasoning = selection.reasoning;
    loadingModels = false;
  }

  async function selectChat(chatId: string): Promise<void> {
    await pageController.selectChat(chatId, { syncUrl: true });
    await syncCommittedDetailUrl(chatId, { mode: 'push' });
  }

  function chatIdFromRowEvent(event: Event, fallbackChatId: string): string {
    const currentTarget = event.currentTarget;
    if (currentTarget instanceof HTMLElement) {
      const rowChatId = currentTarget.dataset.chatId?.trim();
      if (rowChatId) return rowChatId;
    }
    return fallbackChatId;
  }

  function capturePointerChat(event: PointerEvent, fallbackChatId: string): void {
    pendingPointerChatId = chatIdFromRowEvent(event, fallbackChatId);
  }

  function captureDocumentChatPointer(event: PointerEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const row = target.closest<HTMLElement>('.chat-card[data-chat-id]');
    const rowChatId = row?.dataset.chatId?.trim();
    if (rowChatId) pendingPointerChatId = rowChatId;
  }

  function selectPointerChat(event: MouseEvent, fallbackChatId: string): void {
    const targetChatId = pendingPointerChatId ?? chatIdFromRowEvent(event, fallbackChatId);
    pendingPointerChatId = null;
    void selectChat(targetChatId);
  }

  function markActiveChatRead(): void {
    const next = markSessionChatRead(lastSeenMap, activeChatId, chats, localDraftChat);
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-chat:${activeChatId}:${Date.now()}`);
  }

  function markAllUnreadChatsRead(): void {
    const next = markVisibleChatsRead(lastSeenMap, chats);
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-all:${Date.now()}`);
  }

  function invalidateChatMutation(chatId: string): Promise<void> {
    return invalidateChatMutations([chatId]);
  }

  async function invalidateChatMutations(chatIds: string[]): Promise<void> {
    await invalidateReadModelTags([
      readModelEntityTags.chatIndex,
      ...chatIds.map((chatId) => readModelEntityTags.chat(chatId))
    ]);
    await pageController.refreshIndex();
  }

  async function retireChat(chatId: string, options: { confirmed?: boolean } = {}): Promise<void> {
    if (archiving) return;
    if (!options.confirmed) {
      const chat = chatSummaryForId(chatId);
      const ok = await confirmDialog({
        title: 'Retire chat',
        message: `Retire "${chat?.title ?? chatId}"?`,
        confirmText: 'Retire',
        danger: true
      });
      if (!ok) return;
    }
    archiving = true;
    composeError = null;
    const reconciliationId = `retire:${chatId}:${Date.now()}`;
    readModelEntityStore.optimisticRetireChat(chatId, reconciliationId);
    const result = await webApi.pma.retireThread(chatId);
    if (result.ok) {
      await invalidateChatMutation(chatId);
      showCommandNotice('Chat retired.');
      if (activeChatId === chatId) {
        closeStream();
        await goto(chatsHubHref(null));
      }
    } else {
      readModelEntityStore.revertOptimisticMutation(reconciliationId);
      composeError = result.error;
    }
    archiving = false;
  }

  async function retireAllActiveChats(): Promise<void> {
    if (activeChatCount <= 0 || archiving) return;
    const ok = await confirmDialog({
      title: 'Retire active chats',
      message: `Retire ${activeChatCount} active chat${activeChatCount === 1 ? '' : 's'}?`,
      confirmText: 'Retire',
      danger: true
    });
    if (!ok) return;
    archiving = true;
    composeError = null;
    const result = await webApi.pma.retireActiveThreads();
    if (result.ok) {
      const targets = result.data.threads.map((chat) => chat.id);
      await invalidateChatMutations(targets);
      showCommandNotice(
        result.data.errorCount > 0
          ? `Retired ${result.data.retiredCount}; ${result.data.errorCount} failed.`
          : `Retired ${result.data.retiredCount} chats.`
      );
      if (activeChatId && targets.includes(activeChatId)) {
        closeStream();
        await goto(chatsHubHref(null));
      }
    } else {
      composeError = result.error;
    }
    archiving = false;
  }

  async function replaceDetailUrl(detailId: string): Promise<void> {
    const params = new URLSearchParams(page.url.searchParams);
    for (const key of ['chat', 'detail', 'draft', 'new', 'kind']) params.delete(key);
    const target = chatRoute(detailId, { searchParams: params });
    history.replaceState(history.state, '', href(target));
  }

  async function syncCommittedDetailUrl(detailId: string, options: { mode?: 'push' | 'replace' } = {}): Promise<void> {
    if (!detailId) return;
    if (options.mode !== 'push') {
      await replaceDetailUrl(detailId);
      return;
    }
    const params = new URLSearchParams(currentBrowserUrl().searchParams);
    for (const key of ['chat', 'detail', 'draft', 'new', 'kind']) params.delete(key);
    const target = chatRoute(detailId, { searchParams: params });
    history.pushState(history.state, '', href(target));
  }

  async function refreshActive(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    await pageController.refreshActive(chatId, options);
  }

  async function refreshArtifactDeliveries(
    repoId: string | null,
    key = `${activeChatId ?? ''}|${repoId ?? ''}`,
    options: { quiet?: boolean } = {}
  ): Promise<void> {
    if (!options.quiet) loadingArtifactDeliveries = true;
    const result = await webApi.pma.listArtifactDeliveries(repoId);
    if (key !== artifactDeliveryLoadKey) return;
    loadingArtifactDeliveries = false;
    if (result.ok) {
      artifactDeliveryError = null;
      artifactDeliveries = [...result.data].sort(compareArtifactDeliveries);
    } else {
      artifactDeliveryError = result.error;
    }
  }

  function fileBoxScopeForChat(chat: PmaChatSummary | null): ChatFileBoxScope {
    return chat?.repoId ? { kind: 'repo', repoId: chat.repoId } : { kind: 'hub' };
  }

  function fileBoxScopeLabel(scope: ChatFileBoxScope): string {
    return scope.kind === 'repo' ? 'repo filebox' : 'hub filebox';
  }

  async function refreshChatFileBox(options: { quiet?: boolean } = {}): Promise<number | null> {
    const scope = fileBoxScopeForChat(activeChat);
    const result = await webApi.filebox.listFiles(scope);
    if (!result.ok) {
      composeError = result.error;
      return null;
    }
    if (activeChatId) readModelEntityStore.setPmaArtifacts(activeChatId, result.data);
    if (!activeChatId || scope.kind === 'hub') readModelEntityStore.setPmaArtifacts('__global__', result.data);
    if (!options.quiet) showCommandNotice(result.data.length ? `Files refreshed (${result.data.length}).` : 'No files yet.');
    return result.data.length;
  }

  function fileBoxNameFromArtifact(artifact: SurfaceArtifact): FileBoxName | null {
    const box = String(artifact.raw.box ?? '').toLowerCase();
    return box === 'inbox' || box === 'outbox' ? box : null;
  }

  function fileBoxFilenameFromArtifact(artifact: SurfaceArtifact): string | null {
    const rawName = artifact.raw.name;
    return typeof rawName === 'string' && rawName.trim() ? rawName : artifact.title;
  }

  function deletingFileKey(box: FileBoxName, filename: string): string {
    return `${box}:${filename}`;
  }

  function isDeletingFile(box: FileBoxName, filename: string): boolean {
    return deletingFileKeys.has(deletingFileKey(box, filename));
  }

  function setDeletingFile(box: FileBoxName, filename: string, deleting: boolean): void {
    const next = new Set(deletingFileKeys);
    const key = deletingFileKey(box, filename);
    if (deleting) next.add(key);
    else next.delete(key);
    deletingFileKeys = next;
  }

  async function deleteChatFileBoxFile(artifact: SurfaceArtifact): Promise<void> {
    const box = fileBoxNameFromArtifact(artifact);
    const filename = fileBoxFilenameFromArtifact(artifact);
    const scope = fileBoxScopeForChat(activeChat);
    if (!box || !filename) return;
    if (isDeletingFile(box, filename)) return;
    const ok = await confirmDialog({
      title: 'Delete file',
      message: `Delete "${filename}" from ${fileBoxScopeLabel(scope)} ${box}?`,
      confirmText: 'Delete',
      danger: true
    });
    if (!ok) return;
    setDeletingFile(box, filename, true);
    composeError = null;
    const result = await webApi.filebox.deleteFile(scope, box, filename);
    if (result.ok) {
      await refreshChatFileBox({ quiet: true });
      showCommandNotice(`Deleted ${filename}.`);
    } else {
      composeError = result.error;
    }
    setDeletingFile(box, filename, false);
  }

  async function deleteChatFileBox(box: FileBoxName, count: number): Promise<void> {
    if (count <= 0 || deletingFileBox) return;
    const scope = fileBoxScopeForChat(activeChat);
    const ok = await confirmDialog({
      title: `Clear ${box}`,
      message: `Delete all ${count} file${count === 1 ? '' : 's'} from ${fileBoxScopeLabel(scope)} ${box}?`,
      confirmText: 'Delete all',
      danger: true
    });
    if (!ok) return;
    deletingFileBox = box;
    composeError = null;
    const result = await webApi.filebox.deleteBox(scope, box);
    if (result.ok) {
      await refreshChatFileBox({ quiet: true });
      showCommandNotice(`Cleared ${box}.`);
    } else {
      composeError = result.error;
    }
    deletingFileBox = null;
  }

  function compareArtifactDeliveries(left: ArtifactDelivery, right: ArtifactDelivery): number {
    return deliveryTimeValue(right) - deliveryTimeValue(left);
  }

  function deliveryTimeValue(delivery: ArtifactDelivery): number {
    const value = delivery.updatedAt ?? delivery.createdAt ?? delivery.sentAt ?? delivery.failedAt;
    const timestamp = value ? Date.parse(value) : 0;
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function artifactDeliveryStateLabel(delivery: ArtifactDelivery): string {
    const state = delivery.state.trim().toLowerCase();
    if (state === 'claimed' || state === 'sending') return 'sending';
    return state || 'pending';
  }

  function artifactDeliveryMeta(delivery: ArtifactDelivery): string {
    const parts = [
      delivery.targetSurface ? `to ${delivery.targetSurface}` : null,
      delivery.size !== null ? formatBytes(delivery.size) : null,
      delivery.updatedAt ? formatRelativeTime(delivery.updatedAt) : null
    ].filter((part): part is string => Boolean(part));
    return parts.join(' · ');
  }

  function artifactDeliveriesForActiveSurface(
    chat: PmaChatSummary | null,
    deliveries: ArtifactDelivery[]
  ): ArtifactDelivery[] {
    if (!chat) return [];
    const ref = surfaceRefFromThreadRaw(chat.raw as Record<string, unknown>);
    if (!ref) return [];
    const targetKeys = new Set([ref.key, `${ref.kind}:${ref.key}`].map((value) => value.toLowerCase()));
    const surface = ref.kind.toLowerCase();
    return deliveries.filter((delivery) => {
      const deliverySurface = delivery.targetSurface?.toLowerCase() ?? '';
      const deliveryTarget = delivery.targetConversation?.toLowerCase() ?? '';
      return (!deliverySurface || deliverySurface === surface) && targetKeys.has(deliveryTarget);
    });
  }

  async function openFileDrawer(): Promise<void> {
    if (fileDrawerOpen) {
      fileDrawerOpen = false;
      return;
    }
    fileDrawerOpen = true;
    const refreshes: Promise<unknown>[] = [refreshChatFileBox({ quiet: true })];
    if (activeChat) {
      const key = `${activeChat.id}|${activeChat.repoId ?? ''}`;
      artifactDeliveryLoadKey = key;
      refreshes.push(refreshArtifactDeliveries(activeChat.repoId ?? null, key));
    }
    await Promise.all(refreshes);
  }

  function closeStream(): void {
    pageController.closeStream();
  }

  function chatIndexLoadError(): ApiError | null {
    const data = safePageData();
    return data?.chatIndex?.status === 'error' ? data.chatIndex.error : null;
  }

  function apiErrorDetailText(error: ApiError): string | null {
    const parts = [
      error.status ? `HTTP ${error.status}` : null,
      error.code && error.code !== `http_${error.status ?? ''}` ? error.code : null
    ].filter(Boolean);
    if (parts.length === 0) return null;
    return parts.join(' · ');
  }

  function initialRouteSnapshot() {
    const url = currentBrowserUrl();
    return {
      chatId: chatIdFromPath(url.pathname),
      searchParams: url.searchParams,
      data: safePageData()
    };
  }

  function currentRouteSnapshot() {
    const url = currentBrowserUrl();
    return {
      chatId: chatIdFromPath(url.pathname),
      searchParams: url.searchParams,
      data: safePageData()
    };
  }

  function currentBrowserUrl(): URL {
    if (typeof window !== 'undefined') return new URL(window.location.href);
    return page.url;
  }

  function chatIdFromPath(pathname: string): string | null {
    const marker = '/chats/';
    const index = pathname.indexOf(marker);
    if (index < 0) return null;
    const encoded = pathname.slice(index + marker.length).split('/')[0]?.trim();
    return encoded ? decodeURIComponent(encoded) : null;
  }

  function safePageData(): ChatRouteLoadData | undefined {
    try {
      return page.data as ChatRouteLoadData | undefined;
    } catch {
      return undefined;
    }
  }

  function retryStream(): void {
    pageController.retryStream(activeChatId);
  }

  function syncSelectorsToActiveChat(): void {
    const chat = chatSummaryForId(activeChatId);
    // Record what we synced against. When the chat summary has not loaded yet
    // (URL deep-link before the chat index arrives), `chat.agentId` is absent;
    // leave the key null so the re-sync effect runs once the summary lands.
    syncedSelectorKey = chat?.agentId ? `${activeChatId}|${chat.agentId}` : null;
    const scopeId = scopeIdForChat(chat);
    if (scopeId) selectedScopeId = scopeId;
    const resolved = resolvePmaChatSelectorsForActiveChat(
      chat,
      agents,
      configuredDefaultAgentId,
      configuredDefaultProfile
    );
    if (resolved.mode === 'defaults') {
      selectedAgent = resolved.agentId;
      selectedProfile = resolved.agentProfile;
      selectedReasoning = resolved.reasoning;
      void loadModels(selectedAgent, { mode: 'draft' });
      return;
    }
    const previousAgent = selectedAgent;
    selectedAgent = resolved.agentId;
    selectedProfile = resolved.agentProfile;
    selectedReasoning = resolved.reasoning;
    if (previousAgent !== resolved.agentId || models.length === 0) {
      void loadModels(resolved.agentId, {
        preferredModel: resolved.model,
        mode: 'chat-bound'
      });
    } else if (resolved.model) {
      selectedModel = resolved.model;
    } else {
      selectedModel = '';
    }
  }

  function handleAgentChange(): void {
    if (selectedAgent !== 'hermes') selectedProfile = '';
    void loadModels(selectedAgent, { mode: 'draft' });
  }

  function handlePickerChange(): void {
    if (loadingModels) return;
    if (!agentCanListModels(agentRecordForId(agents, selectedAgent))) return;
    if (!selectedModel.trim()) {
      persistLastModelForAgent(selectedAgent, '');
      return;
    }
    if (modelExists(models, selectedModel)) persistLastModelForAgent(selectedAgent, selectedModel);
  }

  function handleScopePickerChange(): void {
    selectedScopeSource = selectedScopeId === 'local' ? 'default_hub' : 'picker_explicit';
    if (newChatKind === 'agent' && selectedScopeId === 'local') newChatKind = 'pma';
    persistCurrentNewChatPreference();
  }

  function firstScopedScopeOption(): PmaChatScopeOption | null {
    return scopeOptions.find((scope) => scope.kind !== 'local') ?? null;
  }

  function handleModePickerChange(): void {
    if (newChatKind === 'agent' && selectedScopeId === 'local') {
      const scoped = firstScopedScopeOption();
      if (scoped) {
        selectedScopeId = scoped.id;
        selectedScopeSource = 'picker_explicit';
      } else {
        newChatKind = 'pma';
      }
    }
    persistCurrentNewChatPreference();
  }

  function ensureCodingAgentScope(): void {
    if (selectedScopeId !== 'local' && scopeOptions.some((scope) => scope.id === selectedScopeId)) return;
    const scoped = firstScopedScopeOption();
    if (scoped) {
      selectedScopeId = scoped.id;
      selectedScopeSource = 'picker_explicit';
    } else {
      newChatKind = 'pma';
    }
  }

  function persistCurrentNewChatPreference(): void {
    persistLastNewChatPreference({
      scopeId: selectedScopeId,
      kind: newChatKind === 'agent' && canStartCodingAgentChat ? 'agent' : 'pma'
    });
  }

  function newChatDisplayName(): string {
    return newChatKind === 'agent' && canStartCodingAgentChat
      ? 'New coding agent chat'
      : 'New chat';
  }

  function newDraftChatSummary(): PmaChatSummary {
    const now = new Date().toISOString();
    const scope = selectedScope;
    const chatKind = newChatKind === 'agent' && canStartCodingAgentChat ? 'coding_agent' : 'pma';
    return {
      id: `pma:${crypto.randomUUID()}`,
      title: newChatDisplayName(),
      lifecycleStatus: 'draft',
      status: 'idle',
      agentId: selectedAgent || null,
      chatKind,
      agentProfile: selectedProfile.trim() || null,
      model: selectedModel.trim() || null,
      repoId: scope.kind === 'repo' ? scope.resourceId : null,
      worktreeId: scope.kind === 'worktree' ? scope.resourceId : null,
      ticketId: null,
      isTicketFlow: false,
      progressPercent: null,
      updatedAt: now,
      raw: {
        draft: true,
        origin: 'web',
        scope_urn: scope.scopeUrn,
        scope_source: selectedScopeSource,
        chat_kind: chatKind
      }
    };
  }

  function worktreeScopeOption(worktreeId: string): Extract<PmaChatScopeOption, { kind: 'worktree' }> | null {
    return scopeOptions.find(
      (scope): scope is Extract<PmaChatScopeOption, { kind: 'worktree' }> =>
        scope.kind === 'worktree' && scope.resourceId === worktreeId
    ) ?? null;
  }

  function scopeIdForChat(chat: PmaChatSummary | null | undefined): string | null {
    if (!chat) return null;
    if (chat.worktreeId) return `worktree:${chat.worktreeId}`;
    if (chat.repoId) return `repo:${chat.repoId}`;
    return 'local';
  }

  function repoIngressForChat(chat: PmaChatSummary | null): { href: string; label: string; detail: string } | null {
    if (!chat) return null;
    if (chat.worktreeId) {
      const repoId = chat.repoId ?? stringField(chat.raw, 'repo_id') ?? stringField(chat.raw, 'parent_repo_id');
      const opt = worktreeScopeOption(chat.worktreeId);
      const parentRepoId = opt?.parentRepoId ?? repoId ?? null;
      return {
        href: worktreeRoute(chat.worktreeId, parentRepoId),
        label: 'Open worktree',
        detail: parentRepoId ? `${parentRepoId} / ${chat.worktreeId}` : chat.worktreeId
      };
    }
    if (chat.repoId) {
      const label = repoLabelForRepoId(chat.repoId) ?? chat.repoId;
      return {
        href: repoRoute(chat.repoId),
        label: 'Open repo',
        detail: label
      };
    }
    return null;
  }

  function activeScopedRoute(kind: 'tickets' | 'contextspace'): string | null {
    if (!activeChat) return null;
    if (activeChat.worktreeId) {
      const parentRepoId = worktreeScopeOption(activeChat.worktreeId)?.parentRepoId ?? activeChat.repoId ?? null;
      return kind === 'tickets'
        ? worktreeTicketRoute(activeChat.worktreeId, parentRepoId)
        : worktreeContextspaceRoute(activeChat.worktreeId, parentRepoId);
    }
    if (activeChat.repoId) {
      return kind === 'tickets'
        ? repoTicketRoute(activeChat.repoId)
        : repoContextspaceRoute(activeChat.repoId);
    }
    return null;
  }

  function messageScroller(): HTMLElement | null {
    return messageStack?.querySelector<HTMLElement>('.chat-transcript-virtual-list') ?? messageStack;
  }

  function isMessageStackNearBottom(): boolean {
    const scroller = messageScroller();
    if (!scroller) return true;
    const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    return distanceFromBottom <= TRANSCRIPT_BOTTOM_THRESHOLD_PX;
  }

  async function scrollMessagesToBottom(): Promise<void> {
    await tick();
    const scroller = messageScroller();
    if (!scroller) return;
    scroller.scrollTop = scroller.scrollHeight;
  }

  function handleMessageScroll(): void {
    // The user's scroll position is the source of truth for follow-bottom.
    // Programmatic scrolls fire this too, but they land at the bottom so
    // followBottom stays true; user-initiated scrolls up flip it false.
    followBottom = isMessageStackNearBottom();
  }

  $effect(() => {
    // Attach scroll listener + ResizeObserver to whichever element is the
    // scroller right now. The ResizeObserver catches growing message content
    // (streaming tokens, live commentary) which doesn't change card count,
    // so the cardCount-keyed effect below would otherwise miss it.
    if (!messageStack) return;
    const scroller = messageScroller();
    if (!scroller) return;
    scroller.addEventListener('scroll', handleMessageScroll, { passive: true });
    messageStackResizeObserver = new ResizeObserver(() => {
      if (followBottom) scroller.scrollTop = scroller.scrollHeight;
    });
    messageStackResizeObserver.observe(scroller);
    if (scroller.firstElementChild) messageStackResizeObserver.observe(scroller.firstElementChild);
    return () => {
      scroller.removeEventListener('scroll', handleMessageScroll);
      messageStackResizeObserver?.disconnect();
      messageStackResizeObserver = null;
    };
  });

  async function createChat(options: { preserveSelectedScope?: boolean; preserveSelectedKind?: boolean } = {}): Promise<void> {
    creating = true;
    composeError = null;
    const requestedKind = newChatKind;
    if (!options.preserveSelectedScope) {
      const applied = applyLastNewChatPreference();
      if (!applied) {
        selectedScopeId = 'local';
        selectedScopeSource = 'default_hub';
        if (!options.preserveSelectedKind) newChatKind = 'pma';
      }
      scopeLocked = false;
    }
    if (options.preserveSelectedKind) {
      newChatKind = requestedKind;
      if (newChatKind === 'agent') ensureCodingAgentScope();
    }
    persistCurrentNewChatPreference();
    const draftChat = newDraftChatSummary();
    writeChatDetailSessionState(startLocalDraftChat(readChatDetailSessionState(), draftChat));
    closeStream();
    void syncCommittedDetailUrl(draftChat.id);
    creating = false;
  }

  async function sendMessage(busyPolicy: 'queue' | 'interrupt' | null = null): Promise<void> {
    await chatSendController.sendMessage(busyPolicy);
  }

  async function interruptWithDraft(): Promise<void> {
    await chatSendController.interruptWithDraft(canInterruptWithDraft);
  }

  async function cancelQueuedTurn(turn: PmaQueuedTurn, options: { confirmed?: boolean } = {}): Promise<void> {
    await chatSendController.cancelQueuedTurn(turn, options);
  }

  async function interruptWithQueuedTurn(turn: PmaQueuedTurn): Promise<void> {
    await chatSendController.interruptWithQueuedTurn(turn);
  }

  async function clearQueueFromPanel(): Promise<void> {
    await chatSendController.clearQueue();
  }

  async function autoCompactActiveThread(chatId: string): Promise<void> {
    if (displayedProgress?.status === 'running') {
      showCommandNotice('Wait for the current turn to finish before auto-compacting.');
      return;
    }
    sending = true;
    showCommandNotice('Generating compact summary...');
    const summaryResult = await webApi.pma.sendMessage(
      chatId,
      {
        ...buildManagedThreadMessagePayload(
          COMPACT_SUMMARY_PROMPT,
          selectedModel,
          false,
          [],
          selectedReasoning,
          selectedProfile,
          'reject'
        ),
        wait_for_confirmation: true,
        defer_execution: false
      }
    );
    if (!summaryResult.ok) {
      composeError = summaryResult.error;
      sending = false;
      return;
    }
    await invalidateChatMutation(chatId);
    const summary = summaryResult.data.text.trim();
    if (!summary) {
      showCommandNotice('Compaction returned an empty summary; current chat was left unchanged.');
      sending = false;
      await refreshActive(chatId, { quiet: true });
      return;
    }
    const compactResult = await webApi.pma.compactThread(chatId, summary);
    if (!compactResult.ok) {
      composeError = compactResult.error;
      sending = false;
      return;
    }
    await invalidateChatMutation(chatId);
    showCommandNotice('Compact summary generated and saved.');
    sending = false;
    await refreshActive(chatId, { quiet: true });
    clearSlashDraft();
  }

  function clearSlashDraft(): void {
    setComposerDraft('');
    markComposerEdited();
    queueMicrotask(() => autosizeComposer());
  }

  function applySlashCommand(spec: SlashCommandSpec): void {
    setComposerDraft(`/${spec.name}${spec.id === 'compact' || spec.id === 'cancel' || spec.id === 'agent' || spec.id === 'model' || spec.id === 'reasoning' || spec.id === 'profile' || spec.id === 'new' ? ' ' : ''}`);
    markComposerEdited();
    queueMicrotask(() => {
      autosizeComposer();
      composerTextarea?.focus();
    });
  }

  async function executeSlashCommand(specOverride?: SlashCommandSpec): Promise<boolean> {
    const parsed = parseSlashCommand(draft);
    const spec = specOverride ?? parsed?.spec ?? null;
    if (!parsed || !spec) return false;
    const disabled = slashSuggestions.find((item) => item.spec.id === spec.id)?.disabledReason ?? null;
    if (disabled) {
      showCommandNotice(disabled);
      return true;
    }
    const args = parsed.args.trim();
    composeError = null;

    if (spec.id === 'help') {
      showCommandNotice(WEB_SLASH_COMMANDS.map((command) => command.usage).join('  '));
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'new') {
      const kind = args.toLowerCase();
      newChatKind = kind === 'agent' || kind === 'coding-agent' || kind === 'newt' ? 'agent' : 'pma';
      clearSlashDraft();
      await createChat({ preserveSelectedKind: true });
      return true;
    }
    if (spec.id === 'newt') {
      newChatKind = 'agent';
      clearSlashDraft();
      await createChat({ preserveSelectedKind: true });
      showCommandNotice('Started a fresh coding chat.');
      return true;
    }
    if (spec.id === 'agent') {
      const next = args.toLowerCase();
      const known = agents.find((record) => agentId(record) === next);
      if (!next || !known) {
        showCommandNotice(`Known agents: ${agents.map((record) => agentId(record)).filter(Boolean).join(', ') || 'none loaded'}`);
        return true;
      }
      selectedAgent = next;
      handleAgentChange();
      showCommandNotice(`Agent set to ${agentLabel(known)}.`);
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'model') {
      const next = args;
      if (!next || next.toLowerCase() === 'default') selectedModel = '';
      else if (!modelExists(models, next)) {
        showCommandNotice(`Known models: ${models.map((record) => modelLabel(record)).join(', ') || 'none loaded'}`);
        return true;
      } else selectedModel = next;
      persistLastModelForAgent(selectedAgent, selectedModel);
      showCommandNotice(selectedModel ? `Model set to ${selectedModel}.` : 'Model override cleared.');
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'reasoning') {
      const next = args.toLowerCase();
      if (!next || next === 'default') selectedReasoning = '';
      else if (!reasoningOptions.includes(next)) {
        showCommandNotice(`Reasoning options: ${reasoningOptions.join(', ') || 'none for this model'}`);
        return true;
      } else selectedReasoning = next;
      showCommandNotice(selectedReasoning ? `Reasoning set to ${selectedReasoning}.` : 'Reasoning override cleared.');
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'profile') {
      const next = args.trim();
      const profileIds = hermesProfileChoices.map((entry) => entry.id);
      if (!next || next.toLowerCase() === 'default') selectedProfile = '';
      else if (!profileIds.includes(next)) {
        showCommandNotice(`Hermes profiles: ${profileIds.join(', ') || 'none loaded'}`);
        return true;
      } else selectedProfile = next;
      showCommandNotice(selectedProfile ? `Profile set to ${selectedProfile}.` : 'Profile cleared.');
      clearSlashDraft();
      return true;
    }

    if (spec.id === 'pma') {
      const mode = args.toLowerCase();
      if (mode === 'off' || mode === 'disable') {
        showCommandNotice('Web chat is always PMA-backed. Open Settings to change hub PMA configuration.');
      } else {
        if (activeChatId) await refreshActive(activeChatId, { quiet: true });
        showCommandNotice('PMA web chat is active.');
      }
      clearSlashDraft();
      return true;
    }

    if (!activeChatId) return false;
    if (spec.destructive) {
      const ok = await confirmDialog({
        title: 'Run command',
        message: `Run ${spec.usage}?`,
        confirmText: 'Run',
        danger: true
      });
      if (!ok) return true;
    }

    if (spec.id === 'status' || spec.id === 'queue') {
      await refreshActive(activeChatId, { quiet: true });
      showCommandNotice(spec.id === 'queue' ? 'Queue refreshed.' : 'Status refreshed.');
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'reset') {
      newChatKind = 'pma';
      clearSlashDraft();
      await createChat({ preserveSelectedKind: true });
      showCommandNotice('Started a fresh replacement chat.');
      return true;
    }
    if (spec.id === 'tickets' || spec.id === 'contextspace') {
      const route = activeScopedRoute(spec.id);
      if (!route) {
        showCommandNotice('Select a repo or worktree chat first.');
        return true;
      }
      clearSlashDraft();
      await goto(href(route));
      return true;
    }
    if (spec.id === 'files') {
      const refreshedCount = await refreshChatFileBox({ quiet: true });
      if (refreshedCount !== null) {
        if (activeChat) {
          const key = `${activeChat.id}|${activeChat.repoId ?? ''}`;
          artifactDeliveryLoadKey = key;
          await refreshArtifactDeliveries(activeChat.repoId ?? null, key, { quiet: true });
        }
        showCommandNotice(refreshedCount ? `Files refreshed (${refreshedCount}).` : 'No files yet.');
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'interrupt') {
      const result = await webApi.pma.interruptThread(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        showCommandNotice('Interrupt requested.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'resume') {
      const result = await webApi.pma.resumeThread(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        await invalidateChatMutation(activeChatId);
        showCommandNotice('Thread resumed.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'compact') {
      if (!args) {
        await autoCompactActiveThread(activeChatId);
        return true;
      }
      const result = await webApi.pma.compactThread(activeChatId, args);
      if (!result.ok) composeError = result.error;
      else {
        await invalidateChatMutation(activeChatId);
        showCommandNotice('Compaction seed saved.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'retire') {
      const archivedId = activeChatId;
      await retireChat(archivedId, { confirmed: true });
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'cancel') {
      const turn = queuedTurns.find((item) => String(item.position) === args || item.managedTurnId === args);
      if (!turn) {
        showCommandNotice('Use /cancel with a queued position or turn id.');
        return true;
      }
      await cancelQueuedTurn(turn, { confirmed: true });
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'clearqueue') {
      const cleared = await chatSendController.clearQueue({ confirmed: true });
      if (cleared) {
        showCommandNotice('Queue cleared.');
        clearSlashDraft();
      }
      return true;
    }
    return false;
  }

  function addFiles(fileList: FileList | File[], kindOverride?: 'image'): void {
    const next = Array.from(fileList).map((file) => ({
      id: `file-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      kind: (kindOverride ?? (file.type.startsWith('image/') ? 'image' : 'file')) as PendingAttachment['kind'],
      title: file.name || 'Pasted screenshot',
      sizeLabel: formatBytes(file.size),
      url: null,
      uploadedName: null,
      uploadState: 'pending' as const,
      file
    }));
    setComposerAttachments([...pendingAttachments, ...next]);
    markComposerEdited();
  }

  function openLinkDialog(): void {
    linkDraft = '';
    linkDialogOpen = true;
  }

  function cancelLinkDialog(): void {
    linkDialogOpen = false;
    linkDraft = '';
  }

  function addLink(): void {
    const href = linkDraft.trim();
    if (!href) return;
    setComposerAttachments([
      ...pendingAttachments,
      {
        id: `link-${Date.now()}`,
        kind: 'link',
        title: href.trim(),
        sizeLabel: null,
        url: href.trim(),
        uploadedName: null,
        uploadState: 'uploaded'
      }
    ]);
    markComposerEdited();
    cancelLinkDialog();
  }

  function removeAttachment(attachmentId: string): void {
    setComposerAttachments(removePendingAttachment(pendingAttachments, attachmentId));
    markComposerEdited();
  }

  function handlePaste(event: ClipboardEvent): void {
    const files = Array.from(event.clipboardData?.files ?? []).filter((file) => file.type.startsWith('image/'));
    if (files.length) addFiles(files, 'image');
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (showSlashCommandMenu && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      slashSelectedIndex = (slashSelectedIndex + delta + slashSuggestions.length) % slashSuggestions.length;
      return;
    }
    if (showSlashCommandMenu && event.key === 'Tab') {
      const selected = slashSuggestions[slashSelectedIndex]?.spec;
      if (selected) {
        event.preventDefault();
        applySlashCommand(selected);
      }
      return;
    }
    if (event.key === 'Escape' && showSlashCommandMenu) {
      event.preventDefault();
      slashSelectedIndex = 0;
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      void submitComposerFromDraft();
    }
  }

  function handleComposerInput(): void {
    setComposerDraft(draft);
    markComposerEdited();
    autosizeComposer();
  }

</script>

<MasterDetail
  label="Chats workspace"
  selected={Boolean(activeChat)}
  mode={detailMode}
  listLabel="Chats"
  detailLabel="Detail"
  showSwitch={false}
  hideDetail={!activeChat}
  onModeChange={(mode) => (detailMode = mode)}
>
  {#snippet list()}
  <aside class="chat-list" aria-label="Chats">
    <div class="chat-list-header">
      <label class="search-field chat-list-search">
        <span class="sr-only">Search chats</span>
        <input bind:value={search} type="search" placeholder="Search chats, repos, tickets" />
      </label>
      <button class="new-chat-button" type="button" onclick={() => createChat()} disabled={creating}>
        {createChatLabel}
      </button>
    </div>

    <div class="chat-filter-bar">
      <div class="chat-filter-bar-primary">
        <div class="chat-filter-group chat-filter-group-status" role="group" aria-label="Status">
          <span class="chat-filter-group-label">Status</span>
          <FilterRow
            rootClass="chat-filter-chips-row"
            ariaLabel="Chat status filters"
            maxRows={1}
            items={CHAT_FILTER_ORDER.filter((item) =>
              item !== 'archived' &&
              shouldShowChatStatusFilterPill(item, filterCounts, statusFilter)
            ).map((item) => ({
              key: `status:${item}`,
              label: filterChipLabel(item),
              count: filterCounts[item],
              active: statusFilter === item,
              title:
                statusFilter === item
                  ? 'Click to clear'
                  : `Filter by ${filterChipLabel(item).toLowerCase()}`,
              onSelect: () => (statusFilter = toggleChatStatusFilter(statusFilter, item))
            }))}
          />
        </div>
        {#if showArchiveFilterToggle}
          <button
            type="button"
            class="chip chat-filter-archive-toggle"
            class:active={statusFilter === 'archived'}
            title={statusFilter === 'archived' ? 'Hide archived chats' : 'Show archived chats'}
            onclick={() => (statusFilter = toggleChatStatusFilter(statusFilter, 'archived'))}
          >
            Archived
            <span>{archiveFilterCount}</span>
          </button>
        {/if}
        {#if hasAdvancedFilterOptions}
          <button
            type="button"
            class="chip chat-filter-more-toggle"
            class:active={facetFiltersExpanded || activeFacetFilterCount > 0}
            aria-expanded={facetFiltersExpanded}
            aria-controls="chat-facet-filters"
            onclick={() => (facetFiltersExpanded = !facetFiltersExpanded)}
          >
            {facetFiltersExpanded ? 'Hide filters' : 'More filters'}
            {#if activeFacetFilterCount > 0}
              <span>{activeFacetFilterCount}</span>
            {/if}
            <span class="chat-filter-more-chevron" aria-hidden="true">{facetFiltersExpanded ? '▴' : '▾'}</span>
          </button>
        {/if}
        <div class="chat-filter-bar-actions">
        {#if hasAnyFilter}
          <button
            class="ghost-button chat-filter-clear"
            type="button"
            onclick={clearAllFilters}
            title="Clear all filters"
          >Clear filters</button>
        {/if}
      {#if statusFilter === 'unread' && filterCounts.unread > 0}
        <button
          class="ghost-button accent"
          type="button"
          onclick={markAllUnreadChatsRead}
          aria-label="Mark all chats as read"
        >
          Mark all as read
        </button>
      {/if}
      {#if activeChatCount > 0 && statusFilter !== 'archived' && statusFilter !== 'drafts'}
        <OverflowMenu
          ariaLabel="Chat list actions"
          triggerTitle="More actions"
          items={[
            {
              label: archiving ? 'Retiring…' : 'Retire all active chats',
              onSelect: () => void retireAllActiveChats(),
              danger: true,
              disabled: archiving,
              ariaLabel: 'Retire all active chats'
            } satisfies OverflowMenuItem
          ]}
        />
      {/if}
        </div>
      </div>
      {#if facetFiltersExpanded && hasAdvancedFilterOptions}
        <div id="chat-facet-filters" class="chat-filter-bar-advanced">
          {#if categoryFilterOptions.length > 0}
            <div class="chat-filter-group" role="group" aria-label="Type">
              <span class="chat-filter-group-label">Type</span>
              <FilterRow
                rootClass="chat-filter-chips-row"
                ariaLabel="Chat category filters"
                maxRows={1}
                items={categoryFilterOptions.map((item) => ({
                  key: `category:${item.key}`,
                  label: item.label,
                  count: item.key === 'ticket_run' ? ticketRunGroupCount : item.count,
                  active: categoryFilter === item.key,
                  title:
                    categoryFilter === item.key
                      ? 'Click to clear'
                      : `Filter by ${item.label.toLowerCase()} (count matches current search and status)`,
                  onSelect: () =>
                    (categoryFilter = toggleChatFacetFilter(categoryFilter, item.key))
                }))}
              />
            </div>
          {/if}
          {#if transportFilterOptions.length > 0}
            <div class="chat-filter-group" role="group" aria-label="Channel">
              <span class="chat-filter-group-label">Channel</span>
              <FilterRow
                rootClass="chat-filter-chips-row"
                ariaLabel="Chat transport filters"
                maxRows={1}
                items={transportFilterOptions.map((item) => ({
                  key: `transport:${item.key}`,
                  label: item.label,
                  count: item.count,
                  active: transportFilter === item.key,
                  title:
                    transportFilter === item.key
                      ? 'Click to clear'
                      : `Filter by ${item.label.toLowerCase()} (count matches current search and status)`,
                  onSelect: () =>
                    (transportFilter = toggleChatFacetFilter(transportFilter, item.key))
                }))}
              />
            </div>
          {/if}
          {#if scopeKindFilterOptions.length > 0}
            <div class="chat-filter-group" role="group" aria-label="Scope">
              <span class="chat-filter-group-label">Scope</span>
              <FilterRow
                rootClass="chat-filter-chips-row"
                ariaLabel="Chat scope filters"
                maxRows={1}
                items={scopeKindFilterOptions.map((item) => ({
                  key: `scope:${item.key}`,
                  label: item.label,
                  count: item.count,
                  active: scopeKindFilter === item.key,
                  title:
                    scopeKindFilter === item.key
                      ? 'Click to clear'
                      : `Filter by ${item.label.toLowerCase()} (count matches current search and status)`,
                  onSelect: () =>
                    (scopeKindFilter = toggleChatFacetFilter(scopeKindFilter, item.key))
                }))}
              />
            </div>
          {/if}
        </div>
      {/if}
    </div>

    {#if hasAnyFilter}
      <div class="chat-filter-summary" aria-live="polite">
        <p class="chat-filter-summary__lead">
          <span class="chat-filter-summary__count">{chatListResultSummary}</span>
          {#each filterSummaryChips as chip (chip.id)}
            <span class="meta-dot" aria-hidden="true">·</span>
            <button
              type="button"
              class="chip chat-filter-summary-chip"
              title="Remove {chip.label} filter"
              onclick={() => clearFilterSummaryChip(chip)}
            >
              {chip.label}
              <span class="chat-filter-summary-chip-clear" aria-hidden="true">×</span>
            </button>
          {/each}
        </p>
      </div>
    {/if}

    {#snippet chatRow(chat: import('$lib/viewModels/domain').PmaChatSummary, nested: boolean)}
      {@const scopeTags = pmaChatScopeTagView(chat, {
        repoLabel: repoLabelForRepoId,
        worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null
      })}
      {@const listScopeAccent = chatListScopeAccentLabel(chat, scopeTags)}
      {@const listScopeAccentHex = listScopeAccent ? repoAccent(listScopeAccent) : null}
      {@const listAgentLabel = agentDisplayForChat(agents, chat)}
      {@const listBadges = pmaChatBadgeViews(chat, { agentLabel: listAgentLabel })}
      <div
        class:active={chat.id === activeChatId}
        class:nested
        class:is-pinned={pinnedChatIds[chat.id] === true}
        class={`chat-card status-${chat.status}`}
        role="button"
        tabindex="0"
        data-chat-id={chat.id}
        aria-current={chat.id === activeChatId ? 'true' : undefined}
        onpointerdowncapture={(event) => capturePointerChat(event, chat.id)}
        onpointercancel={() => {
          if (pendingPointerChatId === chat.id) pendingPointerChatId = null;
        }}
        onclick={(event) => selectPointerChat(event, chat.id)}
        onkeydown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            selectChat(chatIdFromRowEvent(event, chat.id));
          }
        }}
      >
        <button
          class="chat-glyph-slot"
          class:is-pinned={pinnedChatIds[chat.id] === true}
          type="button"
          title={pinnedChatIds[chat.id] === true ? 'Unpin chat' : 'Pin chat'}
          aria-label={pinnedChatIds[chat.id] === true ? `Unpin ${chat.title}` : `Pin ${chat.title}`}
          aria-pressed={pinnedChatIds[chat.id] === true}
          onpointerdown={(event) => event.stopPropagation()}
          onclick={(event) => toggleChatPinned(event, chat.id)}
        >
          {#if listScopeAccent && listScopeAccentHex}
            <span
              class="chat-row-glyph repo-mini-glyph"
              style={`--glyph-accent: ${listScopeAccentHex}`}
              aria-hidden="true"
            >{repoInitials(listScopeAccent)}</span>
          {:else}
            <span class="chat-row-glyph pma-glyph" aria-hidden="true">P</span>
          {/if}
          <span class="chat-pin-glyph" aria-hidden="true">📌</span>
        </button>
        <span class="chat-card-main">
          <span class="chat-row-title">
            {#if isChatUnread(chat, lastSeenMap)}
              <span class="chat-unread-dot" aria-label="Unread"></span>
            {/if}
            <strong class="chat-row-title-text">{nested && chat.ticketId ? chat.ticketId : chat.title}</strong>
            <span class="chat-row-title-trailing">
              {#if chat.status !== 'idle' && chat.status !== 'done'}
                <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
              {/if}
              {#if chat.updatedAt}
                <span class="updated-at">{formatRelativeTime(chat.updatedAt)}</span>
              {/if}
            </span>
          </span>

          {#if !nested}
            <span class="chat-row-tags" role="list" aria-label="Chat tags">
              <span class={`chat-scope-kind-tag ${scopeTags.kindKey}`}>{scopeTags.kindLabel}</span>
              {#if isPmaChatArchived(chat)}
                <span class="chat-scope-kind-tag retired">Retired</span>
              {/if}
              {#if chatHasLocalDraft(chat)}
                <span class="chat-scope-kind-tag draft">Draft</span>
              {/if}
              {#each listBadges as badge}
                <span class={badge.className}>{badge.label}</span>
              {/each}
            </span>
          {/if}

          <span class="chat-row-meta">
            {#if !nested}
              <span
                class="chat-scope-detail-tag"
                style={listScopeAccentHex ? `color: ${listScopeAccentHex}` : undefined}
                title={scopeTags.detailFull ?? scopeTags.detail}
              >{scopeTags.detail}</span>
              {#if chat.ticketId}
                <span class="chat-meta-dot" aria-hidden="true">·</span>
                <code>{chat.ticketId}</code>
              {/if}
              {#if chatModelMetaLabel(chat)}
                <span class="chat-meta-dot" aria-hidden="true">·</span>
                <span
                  class:runtime-unknown={runtimeModelIsExplicitlyUnknown(chat)}
                  class="chat-model"
                  title={chatModelMetaTitle(chat)}
                >{chatModelMetaLabel(chat)}</span>
              {/if}
            {:else}
              {#if chat.title && chat.title !== chat.ticketId}
                <span class="chat-nested-title" title={chat.title}>{chat.title}</span>
              {/if}
              {#if listAgentLabel}
                {#if chat.title && chat.title !== chat.ticketId}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                <span class="chat-agent">{listAgentLabel}</span>
              {/if}
              {#if chatModelMetaLabel(chat)}
                {#if (chat.title && chat.title !== chat.ticketId) || listAgentLabel}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                <span
                  class:runtime-unknown={runtimeModelIsExplicitlyUnknown(chat)}
                  class="chat-model"
                  title={chatModelMetaTitle(chat)}
                >{chatModelMetaLabel(chat)}</span>
              {/if}
            {/if}
          </span>
          {#if chat.progressPercent !== null && Number.isFinite(chat.progressPercent)}
            <span class="chat-card-footer">
              <span
                class={`progress-track status-${chat.status}`}
                aria-label={`${Math.round(chat.progressPercent)} percent complete`}
              >
                <span style={`width: ${progressPercent(chat)}%`}></span>
              </span>
            </span>
          {/if}
        </span>
      </div>
    {/snippet}

    <div class="chat-list-scroll">
      {#if showChatListSkeleton}
        <ContentSkeleton variant="chat-list" rows={6} />
      {:else if visibleChatError}
        <div class="state-panel error">
          <strong>Could not load chats</strong>
          <p>{visibleChatError.message}</p>
          {#if apiErrorDetailText(visibleChatError)}
            <p class="state-panel-detail">{apiErrorDetailText(visibleChatError)}</p>
          {/if}
          <button class="ghost-button" type="button" onclick={() => void pageController.refreshIndex()}>Retry</button>
        </div>
      {:else}
        <VirtualList
          items={filteredEntries}
          key={(entry) => chatListVirtualKey(entry)}
          estimatedItemSize={92}
          overscan={8}
          initialCount={48}
          ariaLabel="Chat rows"
          class="chat-list-virtual-list"
          onEndReached={() => void loadMoreChats()}
        >
        {#snippet children(entry)}
          {#if entry.kind === 'chat'}
            {@render chatRow(entry.chat, false)}
          {:else}
            {@const group = entry.group}
            {@const expanded = isGroupExpanded(group)}
            {@const accentHex = repoAccent(group.scopeLabel)}
            <div class={`chat-run-group status-${group.status}`} class:expanded>
              <button
                class="chat-run-group-header"
                type="button"
                aria-expanded={expanded}
                aria-controls={`run-group-children-${group.key}`}
                onclick={() => toggleGroup(group)}
              >
                <span
                  class="chat-row-glyph repo-mini-glyph chat-run-glyph"
                  style={`--glyph-accent: ${accentHex}`}
                  aria-hidden="true"
                >{repoInitials(group.scopeLabel)}</span>
                <span class="chat-run-group-main">
                  <span class="chat-title-row">
                    <span class="chat-title-cluster">
                      {#if group.unreadCount > 0}
                        <span class="chat-unread-dot" aria-label={`${group.unreadCount} unread`}></span>
                      {/if}
                      <span class="chat-title-text-badge">
                        <strong>{group.scopeLabel}</strong>
                        <span class="chat-scope-kind-tag tickets">Tickets</span>
                        <span class={`chat-scope-kind-tag ${group.scopeKind}`}>{group.scopeKind === 'worktree' ? 'WORKTREE' : 'REPO'}</span>
                        <span class="chat-run-count-chip"><strong>{group.totalCount}</strong> {group.totalCount === 1 ? 'chat' : 'chats'}</span>
                      </span>
                    </span>
                    <span class="chat-title-trailing">
                      {#if group.status !== 'idle' && group.status !== 'done'}
                        <span class={groupBadgeClass(group)}>{statusLabel(group.status)}</span>
                      {/if}
                      {#if group.updatedAt}
                        <span class="updated-at">{formatRelativeTime(group.updatedAt)}</span>
                      {/if}
                      <span class="chat-run-chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
                    </span>
                  </span>
                  <span class="chat-meta-row chat-run-meta">
                    {#each groupSummaryParts(group) as part, i}
                      {#if i > 0}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                      <span>{part}</span>
                    {/each}
                    {#if group.unreadCount > 0}
                      <span class="chat-meta-dot" aria-hidden="true">·</span>
                      <span class="chat-run-unread-text">{group.unreadCount} unread</span>
                    {/if}
                    {#if group.agents.length > 0}
                      <span class="chat-meta-dot" aria-hidden="true">·</span>
                      <span class="chat-agent">{group.agents.join(', ')}</span>
                    {/if}
                  </span>
                </span>
              </button>
              {#if expanded}
                <div id={`run-group-children-${group.key}`} class="chat-run-group-children">
                  <div class="chat-run-group-child-list" role="list" aria-label={`Chats in ${group.scopeLabel} ticket run`}>
                    {#each group.chats as chat (pinAwareChatRowKey(chat))}
                      <div class="chat-run-group-child-row" role="listitem">
                        {@render chatRow(chat, true)}
                      </div>
                    {/each}
                  </div>
                  {#if group.unreadCount > 0}
                    <button
                      type="button"
                      class="ghost-button chat-run-mark-read"
                      onclick={(event) => { event.stopPropagation(); markGroupRead(group); }}
                    >Mark run as read</button>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        {/snippet}
        </VirtualList>
        {#if loadingMoreChats}
          <div class="chat-list-loading-more" role="status">Loading more chats...</div>
        {/if}
        {#if filteredEntries.length === 0}
          <div class="state-panel empty-state compact-empty chat-filter-empty">
            {#if hasAnyFilter}
              <strong>No chats match these filters</strong>
              <p>Counts reflect totals before other filters apply; combining filters can yield no results.</p>
              <button type="button" class="ghost-button chat-filter-clear" onclick={clearAllFilters}>Clear filters</button>
            {:else}
              <strong>No chats yet</strong>
              <p>Start a new chat to populate this list.</p>
            {/if}
          </div>
        {/if}
      {/if}
    </div>
  </aside>
  {/snippet}

  {#snippet detail()}
  {#snippet typingDots(ariaLabel: string)}
    <div class="assistant-skeleton" role="status" aria-label={ariaLabel}>
      <span class="assistant-skeleton-label">{chatAgentDisplayLabel || 'Assistant'}</span>
      <span class="assistant-skeleton-dots" aria-hidden="true">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </span>
    </div>
  {/snippet}
  <div class="active-chat">
    <div class="chat-header">
      <div class="chat-header-copy">
        <h1>{activeChat?.title ?? 'Chats'}</h1>
        {#if activeChat}
          <div class="chat-header-scope-line">
            <span class="chat-header-scope-primary">
              {#if activeRepoIngress}
                <a class="chat-header-scope-link" href={href(activeRepoIngress.href)} target="_blank" rel="noopener noreferrer">
                  <span class="chat-header-scope">{activeRepoIngress.detail}</span>
                  <span class="chat-header-scope-arrow" aria-hidden="true">→</span>
                </a>
              {:else}
                <span class="chat-header-scope">{headerScopeLine}</span>
              {/if}
            </span>
          </div>
          <p class="chat-header-subtitle">
            {#each activeChatBadges as badge}
              <span class={badge.className}>{badge.label}</span>
            {/each}
            {#if activeChat.status === 'done' && displayedProgress?.elapsedSeconds !== null && displayedProgress?.elapsedSeconds !== undefined && statusBar}
              <span class={`status-dot status-${activeChat.status}`} aria-hidden="true"></span>
              <strong class="chat-header-status-strong">{statusLabel(activeChat.status)}</strong>
              {#if statusBar.phase && statusBar.phase.toLowerCase() !== statusLabel(activeChat.status).toLowerCase()}
                <span>{statusBar.phase}</span>
              {/if}
              <span>{statusBar.elapsedLabel}</span>
            {:else if activeChat.status !== 'idle' && !showStatusBar}
              <!-- When the composer status bar is showing, it already carries the
                   live status; don't duplicate the signal in the subtitle. -->
              <span class={`status-dot status-${activeChat.status}`} aria-hidden="true"></span>
              <span>{statusLabel(activeChat.status)}</span>
            {/if}
            {#if activeChat.ticketId}
              <span class="chat-meta-dot" aria-hidden="true">·</span>
              <code>{activeChat.ticketId}</code>
            {/if}
            {#if chatHasActivity}
              <span class="chat-meta-dot" aria-hidden="true">·</span>
              <span class="chat-header-config" title={chatModelMetaTitle(activeChat) ?? 'Locked for this chat'}>
                {activeChatRuntimeConfigLabel(activeChat)}
              </span>
            {/if}
          </p>
        {/if}
      </div>
      {#if activeChat && (showStreamHealthAside || !isPmaChatArchived(activeChat) || activeSharedFileCount > 0 || inboxArtifacts.length > 0 || outboxArtifacts.length > 0)}
        {@const filesItem = {
          label: `Files${activeSharedFileCount > 0 ? ` (${activeSharedFileCount})` : ''}`,
          onSelect: () => void openFileDrawer(),
          ariaLabel: 'Open chat files'
        } satisfies OverflowMenuItem}
        {@const overflowItems = !isPmaChatArchived(activeChat)
          ? [
              filesItem,
              {
                label: archiving ? 'Retiring…' : 'Retire chat',
                onSelect: () => void retireChat(activeChat.id),
                danger: true,
                disabled: archiving,
                ariaLabel: 'Retire this chat'
              } satisfies OverflowMenuItem
            ]
          : [filesItem]}
        <div class="chat-header-tools">
          <OverflowMenu
            ariaLabel="Chat actions"
            triggerTitle="Chat actions"
            items={overflowItems}
          />

          {#if showStreamHealthAside}
            <aside class="chat-header-aside" aria-label="Chat stream status">
              <div class={`stream-health ${streamState}`} role="status">
                <span class="status-dot" aria-hidden="true"></span>
                <span>
                  {#if streamState === 'connecting'}
                    Connecting…
                  {:else}
                    {streamError}
                  {/if}
                </span>
                {#if streamState === 'interrupted'}
                  <button class="ghost-button" type="button" onclick={retryStream}>Reconnect</button>
                {/if}
              </div>
            </aside>
          {/if}
        </div>
      {/if}
    </div>

    <div bind:this={messageStack} class="message-stack" aria-live="off">
      {#if activeError}
        <div class="state-panel error">
          <strong>Could not load this chat</strong>
          <p>{activeError.message}</p>
          <button class="ghost-button" type="button" onclick={() => activeChatId && refreshActive(activeChatId)}>Retry</button>
        </div>
      {:else if !activeChat}
        <div class="state-panel empty-state">
          <strong>No chat is selected</strong>
          <p>Pick a conversation or create a new chat to start work.</p>
          <button class="ghost-button" type="button" onclick={() => (detailMode = 'list')}>Browse chats</button>
        </div>
      {:else if showStartPicker}
        <div class="start-picker" aria-label="Start of chat configuration">
          <ChatThreadPreMessagePickers
            {agents}
            bind:agentValue={selectedAgent}
            bind:profileValue={selectedProfile}
            bind:modelValue={selectedModel}
            bind:reasoningValue={selectedReasoning}
            bind:scopeValue={selectedScopeId}
            bind:modeValue={newChatKind}
            {models}
            {scopeOptions}
            scopeLocked={scopeLocked}
            loading={loadingModels}
            showAgent={showAgentSelector}
            onAgentChange={handleAgentChange}
            onPickerChange={handlePickerChange}
            onModeChange={handleModePickerChange}
            onScopeChange={handleScopePickerChange}
          />
        </div>
      {:else if activeCards.length === 0 && liveActivity}
        {@render typingDots(liveActivity.title)}
      {:else if activeCards.length === 0}
        <div class="state-panel empty-state">
          <strong>No transcript available</strong>
          <p>This chat has no visible timeline yet.</p>
        </div>
      {:else}
        <VirtualList
          items={transcriptListItems}
          key={(item) => item.id}
          estimatedItemSize={220}
          overscan={6}
          initialCount={24}
          ariaLabel="Chat transcript"
          class="chat-transcript-virtual-list"
          itemClass="chat-transcript-virtual-item"
          bottomThresholdPx={TRANSCRIPT_BOTTOM_THRESHOLD_PX}
          onScrollState={({ atBottom }) => { transcriptAtBottom = atBottom; }}
          onReady={(api) => { transcriptApi = api; }}
        >
          {#snippet children(item)}
            {#if item.kind === 'card'}
              <ChatTranscriptCards
                cards={[item.card]}
                assistantLabel={chatAgentDisplayLabel}
                {streamingMessageId}
              />
            {:else if item.kind === 'shared-files'}
              <ChatTranscriptCards
                cards={[]}
                assistantLabel={chatAgentDisplayLabel}
                sharedFiles={assistantSharedFiles}
              />
            {:else}
              {@render typingDots(item.title)}
            {/if}
          {/snippet}
        </VirtualList>
      {/if}
    </div>
    <div class="sr-only" aria-live="polite" aria-atomic="false">{srAnnouncement}</div>

    {#if (activeChat && transcriptListItems.length > 0 && !transcriptAtBottom) || (showStatusBar && statusBar)}
      <div class="composer-overlay-anchor">
        <div class="composer-overlay">
          {#if activeChat && transcriptListItems.length > 0 && !transcriptAtBottom}
            <button
              type="button"
              class="jump-to-latest"
              onclick={() => transcriptApi?.scrollToBottom('smooth')}
              aria-label="Jump to latest message"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 5v14M19 12l-7 7-7-7" />
              </svg>
              Jump to latest
            </button>
          {/if}
          {#if showStatusBar && statusBar}
      <div class={`pma-status-bar composer-status-bar ${statusBar.state}`} aria-label="Turn status">
        <span class="status-dot" aria-hidden="true"></span>
        <strong>{statusLabel(statusBar.state)}</strong>
        {#if statusBar.phase && statusBar.phase.toLowerCase() !== statusLabel(statusBar.state).toLowerCase()}
          <span class="meta meta-phase">{statusBar.phase}</span>
        {/if}
        {#if statusBar.elapsedValue}
          <span class="meta meta-elapsed" title={statusBar.elapsedLabel}>
            <span class="meta-num">{statusBar.elapsedValue}</span><span class="meta-suffix"> elapsed</span>
          </span>
        {/if}
        {#if statusBar.queueDepth > 0}
          <span class="meta meta-queue" title={statusBar.queueDepthLabel}>
            <span class="meta-prefix">queue </span><span class="meta-num">{statusBar.queueDepth}</span>
          </span>
        {/if}
        {#if statusBar.totalTokensFull}
          <span class="meta meta-tokens" title={statusBar.tokenUsageLabel ?? ''}>
            <span class="meta-prefix">tokens </span><span
              class="meta-num meta-num-full">{statusBar.totalTokensFull}</span><span
              class="meta-num meta-num-compact">{statusBar.totalTokensCompact}</span><span
              class="meta-suffix"> total</span>
            {#if statusBar.inputTokensFull}
              <span class="meta-extra"><span class="meta-sep"> · </span><span
                class="meta-num meta-num-full">{statusBar.inputTokensFull}</span><span
                class="meta-num meta-num-compact">{statusBar.inputTokensCompact}</span><span
                class="meta-suffix"> in</span></span>
            {/if}
            {#if statusBar.outputTokensFull}
              <span class="meta-extra"><span class="meta-sep"> · </span><span
                class="meta-num meta-num-full">{statusBar.outputTokensFull}</span><span
                class="meta-num meta-num-compact">{statusBar.outputTokensCompact}</span><span
                class="meta-suffix"> out</span></span>
            {/if}
          </span>
        {/if}
        {#if statusBar.contextRemainingLabel && statusBar.contextRemainingPercent !== null}
          <span class="context-meter" title={`Context remaining ${statusBar.contextRemainingPercent}%`}>
            <span class="context-meter-label">{statusBar.contextRemainingLabel}</span>
            <span class="context-meter-track" aria-hidden="true">
              <span style={`width: ${statusBar.contextRemainingPercent}%`}></span>
            </span>
          </span>
        {/if}
      </div>
          {/if}
        </div>
      </div>
    {/if}

    {#if fileDrawerOpen}
      {@const totalFileCount = inboxArtifacts.length + outboxArtifacts.length + activeSurfaceDeliveries.length}
      <section class="chat-files-drawer" aria-label="Chat files">
        <header class="chat-files-drawer-head">
          <div class="chat-files-drawer-title">
            <h2>Shared files</h2>
            {#if totalFileCount > 0}
              <span class="chat-files-drawer-count">{totalFileCount}</span>
            {/if}
          </div>
          <div class="chat-files-drawer-actions">
            <button
              type="button"
              class="icon-button"
              aria-label={loadingArtifactDeliveries ? 'Refreshing files' : 'Refresh files'}
              title="Refresh"
              disabled={!activeChat || loadingArtifactDeliveries}
              onclick={() => activeChat && void refreshArtifactDeliveries(activeChat.repoId ?? null)}
            >
              <svg class:spinning={loadingArtifactDeliveries} viewBox="0 0 24 24" aria-hidden="true">
                <path d="M21 12a9 9 0 1 1-3-6.7" />
                <path d="M21 4v5h-5" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-button"
              aria-label="Close files"
              title="Close"
              onclick={() => (fileDrawerOpen = false)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </div>
        </header>
        <div class="chat-files-sections">
          <section class="chat-files-section" aria-label="Files from you">
            <div class="chat-files-section-head">
              <h3>From you</h3>
              {#if inboxArtifacts.length > 0}
                <span class="chat-files-section-count">{inboxArtifacts.length}</span>
                <button
                  type="button"
                  class="ghost-button danger chat-file-delete-all"
                  disabled={deletingFileBox !== null}
                  title={deletingFileBox === 'inbox' ? 'Clearing…' : `Clear all ${inboxArtifacts.length}`}
                  aria-label={`Clear all ${inboxArtifacts.length} files from you`}
                  onclick={() => deleteChatFileBox('inbox', inboxArtifacts.length)}
                >
                  {deletingFileBox === 'inbox' ? 'Clearing…' : 'Clear all'}
                </button>
              {/if}
            </div>
            {#if inboxArtifacts.length === 0}
              <p class="chat-files-empty">No uploaded files yet.</p>
            {:else}
              <ul class="chat-files-list">
                {#each inboxArtifacts as artifact (artifact.id)}
                  {@const box = fileBoxNameFromArtifact(artifact)}
                  {@const filename = fileBoxFilenameFromArtifact(artifact)}
                  <li>
                    <span class={`attachment-kind kind-${artifact.kind}`} aria-hidden="true">
                      <svg viewBox="0 0 24 24"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/></svg>
                    </span>
                    <span class="chat-files-list-body">
                      {#if artifact.url}
                        <a href={href(artifact.url)} target="_blank" rel="noopener"><strong>{artifact.title}</strong></a>
                      {:else}
                        <strong>{artifact.title}</strong>
                      {/if}
                      {#if artifact.summary}
                        <em>{artifact.summary}</em>
                      {/if}
                    </span>
                    {#if box && filename}
                      <button
                        type="button"
                        class="chat-file-delete icon-button"
                        disabled={isDeletingFile(box, filename) || deletingFileBox !== null}
                        aria-label={`Delete ${filename}`}
                        title={isDeletingFile(box, filename) ? 'Deleting…' : 'Delete'}
                        onclick={() => deleteChatFileBoxFile(artifact)}
                      >
                        <svg viewBox="0 0 24 24" aria-hidden="true">
                          <path d="M4 7h16" />
                          <path d="M10 11v6M14 11v6" />
                          <path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
                          <path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />
                        </svg>
                      </button>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          </section>
          <section class="chat-files-section" aria-label="Files from assistant">
            <div class="chat-files-section-head">
              <h3>From agent</h3>
              {#if outboxArtifacts.length > 0}
                <span class="chat-files-section-count">{outboxArtifacts.length}</span>
                <button
                  type="button"
                  class="ghost-button danger chat-file-delete-all"
                  disabled={deletingFileBox !== null}
                  title={deletingFileBox === 'outbox' ? 'Clearing…' : `Clear all ${outboxArtifacts.length}`}
                  aria-label={`Clear all ${outboxArtifacts.length} files from agent`}
                  onclick={() => deleteChatFileBox('outbox', outboxArtifacts.length)}
                >
                  {deletingFileBox === 'outbox' ? 'Clearing…' : 'Clear all'}
                </button>
              {/if}
            </div>
            {#if outboxArtifacts.length > 0}
              <ul class="chat-files-list">
                {#each outboxArtifacts as artifact (artifact.id)}
                  {@const box = fileBoxNameFromArtifact(artifact)}
                  {@const filename = fileBoxFilenameFromArtifact(artifact)}
                  <li>
                    <span class={`attachment-kind kind-${artifact.kind}`} aria-hidden="true">
                      <svg viewBox="0 0 24 24"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/></svg>
                    </span>
                    <span class="chat-files-list-body">
                      {#if artifact.url}
                        <a href={href(artifact.url)} target="_blank" rel="noopener"><strong>{artifact.title}</strong></a>
                      {:else}
                        <strong>{artifact.title}</strong>
                      {/if}
                      {#if artifact.summary}
                        <em>{artifact.summary}</em>
                      {/if}
                    </span>
                    {#if box && filename}
                      <button
                        type="button"
                        class="chat-file-delete icon-button"
                        disabled={isDeletingFile(box, filename) || deletingFileBox !== null}
                        aria-label={`Delete ${filename}`}
                        title={isDeletingFile(box, filename) ? 'Deleting…' : 'Delete'}
                        onclick={() => deleteChatFileBoxFile(artifact)}
                      >
                        <svg viewBox="0 0 24 24" aria-hidden="true">
                          <path d="M4 7h16" />
                          <path d="M10 11v6M14 11v6" />
                          <path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
                          <path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />
                        </svg>
                      </button>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
            {#if artifactDeliveryError}
              <p class="chat-files-empty error">{artifactDeliveryError.message}</p>
            {:else if activeSurfaceDeliveries.length === 0 && outboxArtifacts.length === 0}
              <p class="chat-files-empty">No agent-shared files yet.</p>
            {:else if activeSurfaceDeliveries.length > 0}
              <ul class="chat-files-list delivery-list">
                {#each activeSurfaceDeliveries as delivery (delivery.deliveryId)}
                  {@const stateLabel = artifactDeliveryStateLabel(delivery)}
                  <li class={`delivery-${stateLabel}`}>
                    <span class={`delivery-state delivery-state-${stateLabel}`}>
                      <span class="status-dot" aria-hidden="true"></span>
                      {stateLabel}
                    </span>
                    <span class="chat-files-list-body">
                      {#if delivery.downloadUrl}
                        <a href={href(delivery.downloadUrl)} target="_blank" rel="noopener"><strong>{delivery.filename}</strong></a>
                      {:else}
                        <strong>{delivery.filename}</strong>
                      {/if}
                      <em>{artifactDeliveryMeta(delivery)}</em>
                      {#if delivery.lastError}
                        <small>{delivery.lastError}</small>
                      {/if}
                    </span>
                  </li>
                {/each}
              </ul>
            {/if}
          </section>
        </div>
      </section>
    {/if}

    <form
      class="composer"
      onpaste={handlePaste}
      onsubmit={(event) => {
        event.preventDefault();
        void submitComposerFromDraft();
      }}
    >
      {#if showComposerResizeGrip}
        <div
          class="composer-resize-grip"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Drag to resize composer"
          title="Drag to resize · double-click to reset"
          onpointerdown={handleComposerResizeStart}
          ondblclick={resetComposerHeight}
        ><span class="composer-resize-grip-bar" aria-hidden="true"></span></div>
      {/if}
      <input
        bind:this={fileInput}
        class="sr-only"
        type="file"
        multiple
        aria-label="Upload file attachment"
        onchange={(event) => addFiles(event.currentTarget.files ?? [])}
      />
      <input
        bind:this={imageInput}
        class="sr-only"
        type="file"
        accept="image/*"
        multiple
        aria-label="Upload image attachment"
        onchange={(event) => addFiles(event.currentTarget.files ?? [], 'image')}
      />
      <div class="attachment-actions" aria-label="Attachment controls">
        <button class="icon-button attachment-button file" type="button" aria-label="Attach files" title="Attach files" onclick={() => fileInput?.click()}>
          <svg class="attachment-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z" />
            <path d="M14 2v5h5" />
            <path d="M9 13h6" />
            <path d="M9 17h4" />
          </svg>
          <span class="sr-only">Attach files</span>
        </button>
        <button class="icon-button attachment-button image" type="button" aria-label="Attach images" title="Attach images" onclick={() => imageInput?.click()}>
          <svg class="attachment-icon" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3" y="5" width="18" height="14" rx="2" />
            <circle cx="8.5" cy="10" r="1.5" />
            <path d="m21 16-5-5L5 19" />
          </svg>
          <span class="sr-only">Attach images</span>
        </button>
        <button class="icon-button attachment-button link" type="button" aria-label="Attach link" title="Attach link" onclick={openLinkDialog}>
          <svg class="attachment-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" />
            <path d="M14 11a5 5 0 0 0-7.1-.1l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1" />
          </svg>
          <span class="sr-only">Attach link</span>
        </button>
        <VoiceComposerButton
          disabled={!activeChat}
          onTranscript={applyTranscript}
          onError={showVoiceNotice}
        />
      </div>
      {#if queuedTurns.length > 0}
        <div class="queue-panel" aria-label="Queued messages">
          <div class="queue-panel-head">
            <span class="queue-panel-title">
              Queued
              <span class="queue-panel-count">{queuedTurns.length}</span>
            </span>
            <span class="queue-panel-hint">Runs after the current turn</span>
            {#if queuedTurns.length > 1}
              <button
                class="ghost-button danger queue-panel-clear"
                type="button"
                onclick={clearQueueFromPanel}
              >
                Clear all
              </button>
            {/if}
          </div>
          <ul class="queue-list">
            {#each queuedTurns as turn, index (turn.managedTurnId)}
              {@const pending = isOptimisticQueuedTurn(turn)}
              <li class="queue-item" class:pending>
                <span class="queue-item-index" aria-hidden="true">{index + 1}</span>
                <span class="queue-item-text" title={turn.prompt}>{turn.promptPreview || turn.prompt}</span>
                <span class="queue-item-actions">
                  <button
                    class="queue-item-run"
                    type="button"
                    disabled={pending}
                    aria-label={`Interrupt the current turn and run queued message ${index + 1}`}
                    title="Interrupt the current turn and run this next"
                    onclick={() => interruptWithQueuedTurn(turn)}
                  >
                    Interrupt
                  </button>
                  <button
                    class="icon-button queue-item-cancel"
                    type="button"
                    disabled={pending}
                    aria-label={`Cancel queued message ${index + 1}`}
                    title="Cancel this queued message"
                    onclick={() => cancelQueuedTurn(turn)}
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
                      <path d="M6 6l12 12M18 6 6 18" />
                    </svg>
                  </button>
                </span>
              </li>
            {/each}
          </ul>
        </div>
      {/if}
      {#if pendingAttachments.length > 0}
        <div class="pending-attachments" aria-label="Pending attachments">
          {#each pendingAttachments as attachment (attachment.id)}
            <span class={`pending-attachment ${attachment.uploadState}`}>
              <span>{attachment.kind}</span>
              <strong>{attachment.title}</strong>
              {#if attachment.sizeLabel}<em>{attachment.sizeLabel}</em>{/if}
              <button class="icon-button" type="button" aria-label={`Remove ${attachment.title}`} onclick={() => removeAttachment(attachment.id)}>
                <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
                  <path d="M6 6l12 12M18 6 6 18" />
                </svg>
              </button>
            </span>
          {/each}
        </div>
      {/if}
      {#if showSlashCommandMenu}
        <div class="slash-command-menu" role="listbox" aria-label="Slash commands">
          {#each slashSuggestions as item, index (item.spec.id)}
            <button
              type="button"
              class:active={index === slashSelectedIndex}
              disabled={Boolean(item.disabledReason)}
              role="option"
              aria-selected={index === slashSelectedIndex}
              title={item.disabledReason ?? item.spec.usage}
              onmousedown={(event) => event.preventDefault()}
              onclick={() => item.disabledReason ? showCommandNotice(item.disabledReason) : applySlashCommand(item.spec)}
            >
              <span class="slash-command-name">/{item.spec.name}</span>
              <span class="slash-command-copy">
                <strong>{item.spec.title}</strong>
                <em>{item.disabledReason ?? item.spec.description}</em>
              </span>
              <kbd>{item.spec.group}</kbd>
            </button>
          {/each}
        </div>
      {/if}
      <textarea
        bind:this={composerTextarea}
        aria-label={activeChat ? `Message ${composerRecipientLabel(activeChat)}` : 'Message chat'}
        bind:value={draft}
        disabled={!activeChat}
        placeholder={activeChat ? `Message ${composerRecipientLabel(activeChat)}...` : 'Create or select a chat'}
        onkeydown={handleComposerKeydown}
        oninput={handleComposerInput}
        onfocus={() => (composerFocused = true)}
        onblur={() => window.setTimeout(() => (composerFocused = false), 120)}
        rows="1"
      ></textarea>
      {#if canInterruptWithDraft}
        <button class="send-button interrupt-send-button" type="button" onclick={interruptWithDraft}>
          Interrupt
        </button>
      {/if}
      <button
        class="send-button"
        type="submit"
        disabled={!hasRunnableDraft}
        title="Send (⌘/Ctrl+Enter)"
      >
        {sending ? (composerWillQueue ? 'Queueing' : 'Sending') : composerWillQueue ? 'Queue' : 'Send'}
      </button>
    </form>
    {#if linkDialogOpen}
      <div class="modal-backdrop" role="presentation" onclick={cancelLinkDialog}>
        <div
          class="approval-modal link-attachment-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="pma-link-attachment-title"
          tabindex="-1"
          onclick={(event) => event.stopPropagation()}
          onkeydown={(event) => event.stopPropagation()}
        >
          <span class="artifact-type">Attachment</span>
          <h2 id="pma-link-attachment-title">Attach link</h2>
          <label class="link-attachment-field">
            <span>URL</span>
            <input
              bind:value={linkDraft}
              type="url"
              placeholder="https://example.com"
              onkeydown={(event) => {
                if (event.key === 'Escape') cancelLinkDialog();
                if (event.key === 'Enter') {
                  event.preventDefault();
                  addLink();
                }
              }}
            />
          </label>
          <div class="modal-actions">
            <button type="button" class="ghost-button" onclick={cancelLinkDialog}>Cancel</button>
            <button type="button" class="send-button" disabled={!linkDraft.trim()} onclick={addLink}>Attach</button>
          </div>
        </div>
      </div>
    {/if}
    <AutoDismissNotice message={composeError?.message ?? null} tone="danger" />
    <AutoDismissNotice message={voiceNotice} tone="warning" />
    <AutoDismissNotice message={commandNotice} tone="success" />
  </div>
  {/snippet}
</MasterDetail>
