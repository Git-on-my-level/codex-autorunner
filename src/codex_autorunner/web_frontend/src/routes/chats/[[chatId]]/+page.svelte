<script lang="ts">
  import { goto } from '$app/navigation';
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
  import { confirmDialog } from '$lib/components/confirmDialog';
  import { pmaApi, type ApiError, type JsonRecord, type PmaQueuedTurn } from '$lib/api/client';
  import {
    pmaChatSummaryToChatIndexRow,
    chatIndexSession,
    invalidateReadModelTags,
    readModelEntityStore,
    readModelEntityTags,
    type ReadModelLoaderResult,
    selectRepoSummaries,
    selectPmaArtifacts,
    selectPmaChats,
    selectPmaProgress,
    selectPmaQueue,
    selectPmaTimeline,
    selectWorktreeSummaries,
    selectReadMarkers
  } from '$lib/data';
  import {
    executePmaChatCommandPlan,
    planInterruptExistingChat,
    planQueueExistingChat,
    planSendExistingChat,
    planStartAndSendChat
  } from '$lib/application/pmaChatCommands';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { openPmaTailEventSource, type StreamSubscription } from '$lib/api/streaming';
  import {
    repoContextspaceRoute,
    repoRoute,
    repoTicketRoute,
    worktreeContextspaceRoute,
    worktreeRoute,
    worktreeTicketRoute
  } from '$lib/viewModels/routes';
  import { mapPmaRunProgress, mapPmaTimelineItem, pmaTimelineContractFields } from '$lib/viewModels/domain';
  import type {
    PmaChatSummary,
    PmaRunProgress,
    PmaTimelineItem,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    buildPmaChatListEntries,
    buildPmaChatScopeOptions,
    buildPmaLiveActivity,
    buildManagedThreadMessagePayload,
    buildPmaTranscriptCards,
    buildPmaStatusBar,
    chooseActiveChatId,
    composeMessageWithAttachments,
    countTicketRunGroups,
    filterPmaChatEntries,
    formatBytes,
    formatRelativeTime,
    isPmaChatArchived,
    localPmaChatScopeOption,
    PMA_CHAT_FILTER_ORDER,
    PMA_CHAT_TICKET_RUNS_FILTER,
    pmaChatKind,
    pmaChatKindLabel,
    pmaChatBindingKey,
    pmaChatHeaderScopeLine,
    pmaChatMessengerSurface,
    pmaChatScopeTagView,
    pmaChatSurfaceFilterOptions,
    pmaChatSurfaceFilterToken,
    progressPercent,
    optimisticUserTimelineItemFromSend,
    reconcilePmaTimeline,
    removePendingAttachment,
    statusLabel,
    summarizeFilterCounts,
    type DocumentFileIntentPayload,
    type PendingAttachment,
    type PmaCard,
    type PmaChatFilter,
    type PmaChatStatusFilter,
    type PmaChatListEntry,
    type PmaChatRunGroup,
    type PmaChatScopeOption
  } from '$lib/viewModels/pmaChat';
  import {
    isChatUnread,
    loadLastSeenMap,
    markAllChatsRead,
    markChatRead,
    saveLastSeenMap,
    type ChatLastSeenMap
  } from '$lib/viewModels/unread';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import {
    agentProfileEntriesForRecord,
    agentCanListModels,
    agentDisplayForChat,
    agentId,
    agentLabel,
    agentRecordForId,
    firstModelValue,
    modelExists,
    modelLabel,
    modelRecordForValue,
    pickerReasoningOptions,
    resolvePmaChatSelectorsForActiveChat,
    stringField
  } from '$lib/viewModels/modelPickers';
  import { getLastModelForAgent, persistLastModelForAgent } from '$lib/viewModels/lastModelByAgent';
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
  const PINNED_CHATS_STORAGE_KEY = 'car.webHub.pinnedChats.v1';

  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  let unsubscribeChatIndexSession: (() => void) | null = null;
  let activeChatId = $state<string | null>(null);
  // New chats are local drafts until the first send commits agent/scope/model
  // and message in one backend call.
  let localDraftChat = $state<PmaChatSummary | null>(null);
  const persistedChats = $derived<PmaChatSummary[]>(selectPmaChats(readModelState));
  const chats = $derived<PmaChatSummary[]>(localDraftChat ? [localDraftChat, ...persistedChats] : persistedChats);
  const timeline = $derived<PmaTimelineItem[]>(selectPmaTimeline(readModelState, activeChatId));
  const progress = $derived<PmaRunProgress | null>(selectPmaProgress(readModelState, activeChatId));
  const artifacts = $derived<SurfaceArtifact[]>(selectPmaArtifacts(readModelState, activeChatId));
  const queuedTurns = $derived<PmaQueuedTurn[]>(selectPmaQueue(readModelState, activeChatId));
  const lastSeenMap = $derived<ChatLastSeenMap>(selectReadMarkers(readModelState) as ChatLastSeenMap);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let scopeOptions = $state<PmaChatScopeOption[]>(buildPmaChatScopeOptions([], []));
  let pendingAttachments = $state<PendingAttachment[]>([]);
  let configuredDefaultAgentId = $state<string | undefined>(undefined);
  let configuredDefaultProfile = $state('');
  let linkDialogOpen = $state(false);
  let linkDraft = $state('');
  let selectedAgent = $state('codex');
  let selectedModel = $state('');
  let selectedReasoning = $state('');
  let selectedProfile = $state('');
  let selectedScopeId = $state('local');
  let newChatKind = $state<'pma' | 'agent'>('pma');
  let filter = $state<PmaChatFilter>('all');
  let detailMode = $state<'list' | 'detail'>('list');
  let search = $state('');
  let draft = $state('');
  let loadingChats = $state(true);
  let loadingActive = $state(false);
  let sending = $state(false);
  let creating = $state(false);
  let archiving = $state(false);
  let loadingModels = $state(false);
  /** Invalidates in-flight `listAgentModels` results when the user switches agents quickly. */
  let loadModelsSeq = 0;
  let chatError = $state<ApiError | null>(null);
  let activeError = $state<ApiError | null>(null);
  let composeError = $state<ApiError | null>(null);
  let streamState = $state<'idle' | 'connecting' | 'connected' | 'interrupted'>('idle');
  let streamError = $state<string | null>(null);
  let streamSubscription: StreamSubscription | null = null;
  // Tracks which managed turn we've already refreshed-on-terminal for, so the
  // SSE poll's repeated terminal progress payloads don't trigger a refresh per
  // tick while the stream stays open across turns.
  let refreshedTerminalTurnId: string | null = null;
  let fileInput: HTMLInputElement | null = $state(null);
  let imageInput: HTMLInputElement | null = $state(null);
  let messageStack: HTMLDivElement | null = $state(null);
  let composerTextarea: HTMLTextAreaElement | null = $state(null);
  let voiceNotice = $state<string | null>(null);
  let commandNotice = $state<string | null>(null);
  let slashSelectedIndex = $state(0);
  let composerFocused = $state(false);
  let composerEditVersion = 0;
  let pendingPointerChatId: string | null = null;
  let removeDocumentChatPointerCapture: (() => void) | null = null;

  const COMPOSER_DEFAULT_MAX_PX = 360;
  const COMPOSER_MIN_MAX_PX = 120;
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

  function applyTranscript(text: string): void {
    if (!text) return;
    voiceNotice = null;
    const trimmed = text.trim();
    if (!trimmed) return;
    const sep = draft && !/\s$/.test(draft) ? ' ' : '';
    draft = `${draft}${sep}${trimmed}`;
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
  let pendingRefreshTimer: number | null = null;
  let activeClockInterval: number | null = null;
  let activeRefreshSeq = 0;
  let clockNowMs = $state(Date.now());
  let lastScrolledChatId: string | null = null;
  let lastScrolledCardCount = 0;
  let lastScrolledEventCount = 0;
  // Sticky-bottom state: true while the user is parked at (or near) the
  // bottom of the transcript. Once they scroll up, we stop force-scrolling
  // on every content change; scrolling back near the bottom re-arms it.
  let followBottom = true;
  let messageStackResizeObserver: ResizeObserver | null = null;

  const activeChat = $derived(
    activeChatId
      ? chats.find((chat) => chat.id === activeChatId) ?? null
      : null
  );
  let expandedRunGroups = $state<Record<string, boolean>>({});
  let pinnedChatIds = $state<Record<string, true>>({});
  const chatListEntries = $derived(
    buildPmaChatListEntries(chats, {
      lastSeen: lastSeenMap,
      repoLabel: repoLabelForRepoId,
      worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null,
      groupRuns: true
    })
  );
  const filteredEntries = $derived(sortEntriesForPins(filterPmaChatEntries(chatListEntries, filter, search, lastSeenMap), pinnedChatIds));
  const filterCounts = $derived(summarizeFilterCounts(chats, lastSeenMap));
  const surfaceFilterChips = $derived(pmaChatSurfaceFilterOptions(chats));
  const ticketRunGroupCount = $derived(countTicketRunGroups(chats));
  const activeChatCount = $derived(chats.filter((chat) => !isPmaChatArchived(chat)).length);
  const hasUsableChatIndex = $derived(Boolean(readModelState.chatIndexCursor || readModelState.chatOrder.length > 0));
  const initialChatIndexError = $derived(chatIndexLoadError());
  const visibleChatError = $derived(chatError ?? (!hasUsableChatIndex ? initialChatIndexError : null));
  const showChatListSkeleton = $derived(loadingChats && !hasUsableChatIndex && !visibleChatError);

  function isGroupExpanded(group: PmaChatRunGroup): boolean {
    if (group.key in expandedRunGroups) return expandedRunGroups[group.key];
    // Default collapsed; expand only when the active chat lives inside this group.
    if (activeChatId && group.chats.some((chat) => chat.id === activeChatId)) return true;
    return false;
  }

  function toggleGroup(group: PmaChatRunGroup): void {
    expandedRunGroups = { ...expandedRunGroups, [group.key]: !isGroupExpanded(group) };
  }

  function loadPinnedChats(): Record<string, true> {
    try {
      const raw = localStorage.getItem(PINNED_CHATS_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return {};
      return Object.fromEntries(parsed.filter((id): id is string => typeof id === 'string' && id.trim().length > 0).map((id) => [id, true]));
    } catch {
      return {};
    }
  }

  function savePinnedChats(next: Record<string, true>): void {
    try {
      localStorage.setItem(PINNED_CHATS_STORAGE_KEY, JSON.stringify(Object.keys(next).sort()));
    } catch {
      // Private mode / quota.
    }
  }

  function toggleChatPinned(event: MouseEvent, chatId: string): void {
    event.preventDefault();
    event.stopPropagation();
    pendingPointerChatId = null;
    const next = { ...pinnedChatIds };
    if (next[chatId]) delete next[chatId];
    else next[chatId] = true;
    pinnedChatIds = next;
    savePinnedChats(next);
  }

  /** VirtualList keys must change when pin state or pin-driven order changes, or keyed {#each} reuses stale row DOM. */
  function chatListVirtualKey(entry: PmaChatListEntry): string {
    if (entry.kind === 'group') {
      const pinOrder = entry.group.chats
        .map((c) => `${c.id}:${pinnedChatIds[c.id] ? 1 : 0}`)
        .join('|');
      return `group:${entry.group.key}:${pinOrder}`;
    }
    return `chat:${entry.chat.id}:${pinnedChatIds[entry.chat.id] ? 1 : 0}`;
  }

  function pinAwareChatRowKey(chat: PmaChatSummary): string {
    return `${chat.id}:${pinnedChatIds[chat.id] ? 1 : 0}`;
  }

  function sortEntriesForPins(entries: PmaChatListEntry[], pinned: Record<string, true>): PmaChatListEntry[] {
    const decorated = entries.map((entry) => {
      if (entry.kind === 'chat') {
        return { entry, pinned: pinned[entry.chat.id] === true, sort: entry.chat.updatedAt ?? '', id: entry.chat.id };
      }
      const chats = [...entry.group.chats].sort((left, right) => {
        const pinnedDiff = Number(pinned[right.id] === true) - Number(pinned[left.id] === true);
        if (pinnedDiff !== 0) return pinnedDiff;
        return (right.updatedAt ?? '').localeCompare(left.updatedAt ?? '');
      });
      return {
        entry: { kind: 'group' as const, group: { ...entry.group, chats } },
        pinned: chats.some((chat) => pinned[chat.id] === true),
        sort: entry.group.updatedAt ?? '',
        id: entry.group.key
      };
    });
    decorated.sort((left, right) => {
      const pinnedDiff = Number(right.pinned) - Number(left.pinned);
      if (pinnedDiff !== 0) return pinnedDiff;
      const timeDiff = right.sort.localeCompare(left.sort);
      if (timeDiff !== 0) return timeDiff;
      return left.id.localeCompare(right.id);
    });
    return decorated.map((item) => item.entry);
  }

  function markGroupRead(group: PmaChatRunGroup): void {
    let next = lastSeenMap;
    const now = new Date().toISOString();
    for (const chat of group.chats) {
      if (!chat.updatedAt && !next[chat.id]) {
        next = markChatRead(next, chat.id, now);
        continue;
      }
      const stamp = chat.updatedAt ?? now;
      if (next[chat.id] && next[chat.id] >= stamp) continue;
      next = markChatRead(next, chat.id, stamp);
    }
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-group:${group.key}:${Date.now()}`);
    saveLastSeenMap(next);
  }

  function groupBadgeClass(group: PmaChatRunGroup): string {
    return `chat-run-status-pill ${group.status}`;
  }

  function groupSummaryParts(group: PmaChatRunGroup): string[] {
    const parts: string[] = [];
    if (group.waitingCount > 0) parts.push(`${group.waitingCount} waiting`);
    if (group.activeCount > 0) parts.push(`${group.activeCount} active`);
    if (group.failedCount > 0) parts.push(`${group.failedCount} failed`);
    parts.push(`${group.doneCount}/${group.totalCount} done`);
    return parts;
  }
  const displayedProgress = $derived(progressWithLiveElapsed(progress, clockNowMs));
  const liveActivity = $derived(buildPmaLiveActivity(displayedProgress));
  const activeCards = $derived<PmaCard[]>(buildPmaTranscriptCards(timeline, activeChat, artifacts, displayedProgress));
  const lastAssistantMessageCard = $derived.by<PmaCard | null>(() => {
    for (let i = activeCards.length - 1; i >= 0; i -= 1) {
      const card = activeCards[i];
      if (card.kind === 'message' && card.message.role === 'assistant') {
        return card;
      }
    }
    return null;
  });
  const statusBar = $derived(buildPmaStatusBar(displayedProgress, activeChat));
  const selectedScope = $derived(scopeOptions.find((scope) => scope.id === selectedScopeId) ?? localPmaChatScopeOption());
  const selectedAgentRecord = $derived(agentRecordForId(agents, selectedAgent));
  const hermesProfileChoices = $derived(agentProfileEntriesForRecord(selectedAgentRecord));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const selectedModelRecord = $derived(modelRecordForValue(models, selectedModel));
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
  const streamingMessageId = $derived.by<string | null>(() => {
    if (displayedProgress?.status !== 'running') return null;
    const card = lastAssistantMessageCard;
    return card?.kind === 'message' ? card.id : null;
  });
  const showTypingIndicator = $derived.by<boolean>(() => {
    if (displayedProgress?.status !== 'running') return false;
    const last = activeCards[activeCards.length - 1];
    if (!last) return false;
    return last.kind === 'message' && last.message.role === 'user';
  });
  const srAnnouncement = $derived.by<string>(() => {
    if (displayedProgress?.status !== 'running') return '';
    const card = lastAssistantMessageCard;
    if (!card || card.kind !== 'message') return '';
    const text = (card.message.text ?? '').trim();
    return text.length > 120 ? text.slice(text.length - 120) : text;
  });
  const activeMessengerSurface = $derived(pmaChatMessengerSurface(activeChat));
  const activeRepoIngress = $derived(repoIngressForChat(activeChat));
  const createChatLabel = $derived(
    creating ? 'Creating...' : newChatKind === 'agent' && canStartCodingAgentChat ? '+ Coding agent' : '+ Chat'
  );
  const headerScopeLine = $derived(pmaChatHeaderScopeLine(activeChat, repoLabelForRepoId));
  /** Omit connected “Live · …” — redundant with the turn-status pill on the scope row. */
  const showStreamHealthAside = $derived(
    streamState === 'connecting' || streamState === 'interrupted'
  );
  const showStatusBar = $derived(
    Boolean(
      statusBar &&
        statusBar.state !== 'idle' &&
        (statusBar.state === 'done'
          ? Boolean(statusBar.tokenUsageLabel || statusBar.contextRemainingLabel)
            : (displayedProgress?.elapsedSeconds !== null && displayedProgress?.elapsedSeconds !== undefined) ||
            (displayedProgress?.queueDepth ?? 0) > 0 ||
            statusBar.tokenUsageLabel ||
            statusBar.contextRemainingLabel ||
            ['running', 'waiting', 'blocked', 'failed'].includes(statusBar.state))
    )
  );
  const chatHasActivity = $derived(activeCards.length > 0 || showStatusBar);
  const showStartPicker = $derived(Boolean(activeChat) && !loadingActive && !activeError && !chatHasActivity);
  const hasRunnableDraft = $derived(Boolean(activeChat && (draft.trim() || pendingAttachments.length > 0)));
  const canInterruptWithDraft = $derived(Boolean(activeChat && displayedProgress?.status === 'running' && hasRunnableDraft));
  const slashSuggestions = $derived<SlashCommandSuggestion[]>(
    buildSlashCommandSuggestions(draft, {
      hasActiveChat: Boolean(activeChat),
      hasScopedWorkspace: selectedScope.kind !== 'local',
      isRunning: displayedProgress?.status === 'running',
      queueDepth: queuedTurns.length || displayedProgress?.queueDepth || 0
    })
  );
  const showSlashCommandMenu = $derived(slashSuggestions.length > 0 && composerFocused);
  const selectedAgentLabel = $derived(selectedAgentRecord ? agentLabel(selectedAgentRecord) : selectedAgent || 'Agent');
  const selectedModelLabel = $derived(selectedModelRecord ? modelLabel(selectedModelRecord) : selectedModel || '');
  const selectedEffortLabel = $derived(selectedReasoning || 'default');

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

  function filterChipLabel(key: PmaChatStatusFilter): string {
    return key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1);
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

  onMount(() => {
    document.addEventListener('pointerdown', captureDocumentChatPointer, true);
    removeDocumentChatPointerCapture = () => {
      document.removeEventListener('pointerdown', captureDocumentChatPointer, true);
    };
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      const replacementChatId = replacementForActiveChat(readModelState, state);
      readModelState = state;
      if (state.chatIndexCursor || state.chatOrder.length > 0) {
        loadingChats = false;
        chatError = null;
        activateRequestedChatFromCurrentRows();
      }
      if (replacementChatId) void selectChat(replacementChatId);
    });
    unsubscribeChatIndexSession = chatIndexSession.state.subscribe((session) => {
      if (session.status === 'loading' && !readModelEntityStore.snapshot().chatIndexCursor) {
        loadingChats = true;
      }
      if (session.error) {
        chatError = session.error;
        loadingChats = false;
      }
    });
    readModelEntityStore.setReadMarkers(loadLastSeenMap());
    pinnedChatIds = loadPinnedChats();
    draft = page.url.searchParams.get('draft') ?? draft;
    loadingChats = !hasChatIndexProjection(readModelEntityStore.snapshot());
    if (!loadingChats) activateRequestedChatFromCurrentRows();
    void loadInitialSupportingData(
      pmaApi.pma.listFiles(),
      pmaApi.pma.listAgents(),
      pmaApi.readModels.repoWorktreeTopology('all', 200),
      pmaApi.readModels.repoWorktreeRuntime('all', 200)
    );
    activeClockInterval = window.setInterval(() => {
      if (progress?.status === 'running') clockNowMs = Date.now();
    }, 1000);
  });

  $effect(() => {
    const requestedDetail = requestedDetailFromUrl();
    if (!requestedDetail) {
      if (isLocalDraftChatId(activeChatId)) return;
      if (sending || creating) return;
      if (activeChatId !== null) {
        closeStream();
        activeChatId = null;
        detailMode = 'list';
      }
      return;
    }
    void activateDetailFromUrl(requestedDetail);
  });

  $effect(() => {
    if (!canStartCodingAgentChat && newChatKind === 'agent') newChatKind = 'pma';
  });

  $effect(() => {
    if (selectedReasoning && !reasoningOptions.includes(selectedReasoning)) selectedReasoning = '';
  });

  $effect(() => {
    if (!activeChat) return;
    const stamp = activeChat.updatedAt;
    if (!stamp) return;
    const seen = lastSeenMap[activeChat.id];
    if (seen && seen >= stamp) return;
    const next = markChatRead(lastSeenMap, activeChat.id, stamp);
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-active:${activeChat.id}:${Date.now()}`);
    saveLastSeenMap(next);
  });

  onDestroy(() => {
    removeDocumentChatPointerCapture?.();
    unsubscribeReadModels?.();
    unsubscribeChatIndexSession?.();
    if (pendingRefreshTimer) window.clearTimeout(pendingRefreshTimer);
    if (activeClockInterval) window.clearInterval(activeClockInterval);
    closeStream();
  });

  $effect(() => {
    const cardCount = activeCards.length;
    const eventCount = progress?.events.length ?? 0;
    const chatChanged = activeChatId !== lastScrolledChatId;
    const cardCountChanged = cardCount !== lastScrolledCardCount;
    const eventCountChanged = eventCount !== lastScrolledEventCount;

    if (!activeChat || loadingActive || (!chatChanged && !cardCountChanged && !eventCountChanged)) return;

    // Switching to a new chat re-arms sticky-bottom; otherwise let the
    // user's current followBottom state decide.
    if (chatChanged) followBottom = true;
    lastScrolledChatId = activeChatId;
    lastScrolledCardCount = cardCount;
    lastScrolledEventCount = eventCount;
    if (followBottom) void scrollMessagesToBottom();
  });

  function activateRequestedChatFromCurrentRows(): void {
    if (isLocalDraftChatId(activeChatId)) return;
    const loadedChats = selectPmaChats(readModelEntityStore.snapshot());
    const requestedChat = page.params.chatId ?? page.url.searchParams.get('chat');
    const selectedChatId = chooseActiveChatId(loadedChats, activeChatId, requestedChat);
    if (!selectedChatId || activeChatId === selectedChatId) return;
    activeChatId = selectedChatId;
    detailMode = 'detail';
    const selected = loadedChats.find((chat) => chat.id === activeChatId);
    if (selected && isPmaChatArchived(selected)) filter = 'archived';
    syncSelectorsToActiveChat();
    connectStream(selectedChatId);
    void refreshActive(selectedChatId, { quiet: hasCachedDetail(selectedChatId) });
  }

  function replacementForActiveChat(
    previousState: typeof readModelState,
    nextState: typeof readModelState
  ): string | null {
    if (!activeChatId) return null;
    const previousActive = selectPmaChats(previousState).find((chat) => chat.id === activeChatId) ?? null;
    const previousBinding = pmaChatBindingKey(previousActive);
    if (!previousBinding) return null;
    const nextChats = selectPmaChats(nextState);
    const nextActive = nextChats.find((chat) => chat.id === activeChatId) ?? null;
    if (nextActive && !isPmaChatArchived(nextActive)) return null;
    const replacement = nextChats.find(
      (chat) => chat.id !== activeChatId && pmaChatBindingKey(chat) === previousBinding && !isPmaChatArchived(chat)
    );
    return replacement?.id ?? null;
  }

  async function loadInitialSupportingData(
    artifactPromise: ReturnType<typeof pmaApi.pma.listFiles>,
    agentPromise: ReturnType<typeof pmaApi.pma.listAgents>,
    topologyPromise: ReturnType<typeof pmaApi.readModels.repoWorktreeTopology>,
    runtimePromise: ReturnType<typeof pmaApi.readModels.repoWorktreeRuntime>
  ): Promise<void> {
    const [artifactResult, agentResult, topologyResult, runtimeResult] = await Promise.all([
      artifactPromise,
      agentPromise,
      topologyPromise,
      runtimePromise
    ]);
    if (artifactResult.ok) readModelEntityStore.setPmaArtifacts('__global__', artifactResult.data);
    if (topologyResult.ok) readModelEntityStore.applyRepoWorktreeTopologySnapshot(topologyResult.data);
    if (runtimeResult.ok) readModelEntityStore.applyRepoWorktreeRuntimeSnapshot(runtimeResult.data);
    const scopeState = readModelEntityStore.snapshot();
    scopeOptions = buildPmaChatScopeOptions(
      topologyResult.ok ? selectRepoSummaries(scopeState) : [],
      topologyResult.ok ? selectWorktreeSummaries(scopeState) : []
    );
    if (!scopeOptions.some((scope) => scope.id === selectedScopeId)) selectedScopeId = 'local';
    if (!canStartCodingAgentChat) newChatKind = 'pma';
    if (agentResult.ok) {
      agents = agentResult.data.agents;
      const defaults = agentResult.data.defaults;
      const defaultAgent =
        typeof defaults.agent === 'string' && defaults.agent.trim()
          ? defaults.agent.trim().toLowerCase()
          : agentResult.data.default;
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
      void loadModels(selectedAgent, activeChat?.model ?? selectedModel);
    }
    applyNewChatQueryParam();
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
        appliedScope = true;
      }
    } else if (decoded.startsWith('worktree:')) {
      const sid = `worktree:${decoded.slice('worktree:'.length)}`;
      if (scopeOptions.some((scope) => scope.id === sid)) {
        selectedScopeId = sid;
        appliedScope = true;
      }
    } else if (decoded === 'local' || decoded === 'hub') {
      selectedScopeId = 'local';
      appliedScope = true;
    }
    const requestedKind = page.url.searchParams.get('kind');
    newChatKind = requestedKind === 'agent' && selectedScopeId !== 'local' ? 'agent' : 'pma';
    const params = new URLSearchParams(page.url.searchParams);
    params.delete('new');
    params.delete('kind');
    const query = params.toString();
    void goto(href(`/chats${query ? `?${query}` : ''}`), { replaceState: true }).then(() => {
      if (appliedScope) void createChat();
    });
  }

  async function loadModels(agentId: string, preferredModel?: string): Promise<void> {
    if (!agentCanListModels(agentRecordForId(agents, agentId))) {
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
    selectedModel = '';
    selectedReasoning = '';

    const result = await pmaApi.pma.listAgentModels(agentId);
    if (seq !== loadModelsSeq) return;

    if (!result.ok) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    models = result.data;
    const remembered = getLastModelForAgent(agentId);
    const pref = preferredModel?.trim();
    let pick = '';
    if (pref && modelExists(result.data, pref)) pick = pref;
    else if (remembered && modelExists(result.data, remembered)) pick = remembered;
    else pick = firstModelValue(result.data);
    selectedModel = pick;
    if (selectedReasoning && !pickerReasoningOptions(result.data, selectedModel).includes(selectedReasoning)) {
      selectedReasoning = '';
    }
    persistLastModelForAgent(agentId, selectedModel);
    loadingModels = false;
  }

  async function selectChat(chatId: string): Promise<void> {
    const cached = hasCachedDetail(chatId);
    if (!isLocalDraftChatId(chatId)) localDraftChat = null;
    activeChatId = chatId;
    detailMode = 'detail';
    loadingActive = !cached;
    activeError = null;
    syncSelectorsToActiveChat();
    markActiveChatRead();
    connectStream(chatId);
    const urlPromise = syncDetailUrl(chatId);
    void refreshActive(chatId, { quiet: cached });
    await urlPromise;
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

  function requestedDetailFromUrl(): string | null {
    if (page.params.chatId) return page.params.chatId;
    const detail = page.url.searchParams.get('detail');
    if (detail?.startsWith('chat:')) return detail.slice('chat:'.length);
    return page.url.searchParams.get('chat');
  }

  async function activateDetailFromUrl(detailId: string): Promise<void> {
    if (detailId === activeChatId) return;
    if (!chats.some((chat) => chat.id === detailId)) {
      closeStream();
      activeChatId = detailId;
      detailMode = 'detail';
      loadingActive = true;
      const loaderResult = activeDetailLoadResult(detailId);
      activeError = loaderResult?.status === 'error' ? loaderResult.error : null;
      if (activeError) {
        loadingActive = false;
        return;
      }
      connectStream(detailId);
      void refreshActive(detailId);
      return;
    }
    await selectChatWithoutUrl(detailId);
  }

  async function selectChatWithoutUrl(chatId: string): Promise<void> {
    const cached = hasCachedDetail(chatId);
    const loaderResult = activeDetailLoadResult(chatId);
    const loaderOwnsInitialDetail = Boolean(loaderResult && loaderResult.status !== 'cold');
    if (!isLocalDraftChatId(chatId)) localDraftChat = null;
    activeChatId = chatId;
    detailMode = 'detail';
    loadingActive = !(cached || loaderOwnsInitialDetail);
    activeError = loaderResult?.status === 'error' ? loaderResult.error : null;
    syncSelectorsToActiveChat();
    markActiveChatRead();
    connectStream(chatId);
    if (!loaderOwnsInitialDetail) void refreshActive(chatId, { quiet: cached });
  }

  function markActiveChatRead(): void {
    if (!activeChatId) return;
    const chat = chats.find((c) => c.id === activeChatId);
    const stamp = chat?.updatedAt ?? new Date().toISOString();
    const next = markChatRead(lastSeenMap, activeChatId, stamp);
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-chat:${activeChatId}:${Date.now()}`);
    saveLastSeenMap(next);
  }

  function markAllUnreadChatsRead(): void {
    const next = markAllChatsRead(lastSeenMap, chats.filter((chat) => !isPmaChatArchived(chat)));
    if (next === lastSeenMap) return;
    readModelEntityStore.optimisticReadMarkers(next, `read-all:${Date.now()}`);
    saveLastSeenMap(next);
  }

  function invalidateChatMutation(chatId: string): Promise<void> {
    return invalidateReadModelTags([
      readModelEntityTags.chatIndex,
      readModelEntityTags.chat(chatId)
    ]);
  }

  async function archiveChat(chatId: string, options: { confirmed?: boolean } = {}): Promise<void> {
    if (archiving) return;
    if (!options.confirmed) {
      const chat = chats.find((item) => item.id === chatId);
      const ok = await confirmDialog({
        title: 'Archive chat',
        message: `Archive "${chat?.title ?? chatId}"?`,
        confirmText: 'Archive',
        danger: true
      });
      if (!ok) return;
    }
    archiving = true;
    composeError = null;
    const reconciliationId = `archive:${chatId}:${Date.now()}`;
    readModelEntityStore.optimisticArchiveChat(chatId, reconciliationId);
    const result = await pmaApi.pma.archiveThread(chatId);
    if (result.ok) {
      await invalidateChatMutation(chatId);
      upsertPmaChats([result.data]);
      showCommandNotice('Chat archived.');
      if (activeChatId === chatId) {
        closeStream();
        await goto(href('/chats'));
      }
    } else {
      readModelEntityStore.revertOptimisticMutation(reconciliationId);
      composeError = result.error;
    }
    archiving = false;
  }

  async function archiveAllActiveChats(): Promise<void> {
    const targets = chats.filter((chat) => !isPmaChatArchived(chat)).map((chat) => chat.id);
    if (!targets.length || archiving) return;
    const ok = await confirmDialog({
      title: 'Archive active chats',
      message: `Archive ${targets.length} active chat${targets.length === 1 ? '' : 's'}?`,
      confirmText: 'Archive',
      danger: true
    });
    if (!ok) return;
    archiving = true;
    composeError = null;
    const result = await pmaApi.pma.archiveThreads(targets);
    if (result.ok) {
      await invalidateReadModelTags([
        readModelEntityTags.chatIndex,
        ...targets.map((chatId) => readModelEntityTags.chat(chatId))
      ]);
      upsertPmaChats(result.data.threads);
      showCommandNotice(
        result.data.errorCount > 0
          ? `Archived ${result.data.archivedCount}; ${result.data.errorCount} failed.`
          : `Archived ${result.data.archivedCount} chats.`
      );
      if (activeChatId && targets.includes(activeChatId)) {
        closeStream();
        await goto(href('/chats'));
      }
    } else {
      composeError = result.error;
    }
    archiving = false;
  }

  async function syncDetailUrl(detailId: string): Promise<void> {
    const params = new URLSearchParams(page.url.searchParams);
    params.delete('draft');
    params.delete('detail');
    params.delete('chat');
    const query = params.toString();
    const target = `/chats/${encodeURIComponent(detailId)}${query ? `?${query}` : ''}`;
    await goto(href(target), { keepFocus: true, noScroll: true });
  }

  async function refreshActive(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    const refreshSeq = ++activeRefreshSeq;
    if (!options.quiet) {
      loadingActive = true;
      activeError = null;
    }
    let missingThreadError: ApiError | null = null;
    const timelineTask = pmaApi.pma.getTimeline(chatId).then((messageResult) => {
      if (activeChatId !== chatId || refreshSeq !== activeRefreshSeq) return;
      if (messageResult.ok) {
        readModelEntityStore.replacePmaTimeline(chatId, reconcilePmaTimeline(currentTimeline(chatId), messageResult.data));
      } else if (isMissingManagedThreadError(messageResult.error)) {
        missingThreadError = messageResult.error;
        readModelEntityStore.replacePmaTimeline(chatId, []);
      } else if (!options.quiet) {
        activeError = messageResult.error;
      }
    });
    const progressTask = Promise.all([pmaApi.pma.getTail(chatId), pmaApi.pma.getStatus(chatId)]).then(
      ([tailResult, statusResult]) => {
        if (activeChatId !== chatId || refreshSeq !== activeRefreshSeq) return;
        if (tailResult.ok) updateProgress(tailResult.data);
        else if (statusResult.ok) updateProgress(statusResult.data);
        else if (isMissingManagedThreadError(tailResult.error) || isMissingManagedThreadError(statusResult.error)) {
          missingThreadError = isMissingManagedThreadError(tailResult.error) ? tailResult.error : statusResult.error;
          readModelEntityStore.setPmaProgress(chatId, null);
        } else if (!options.quiet && !activeError) activeError = tailResult.error;
      }
    );
    const queueTask = pmaApi.pma.getQueue(chatId).then((queueResult) => {
      if (activeChatId !== chatId || refreshSeq !== activeRefreshSeq) return;
      if (queueResult.ok) {
        readModelEntityStore.setPmaQueue(chatId, queueResult.data.queuedTurns);
      } else if (isMissingManagedThreadError(queueResult.error)) {
        missingThreadError = queueResult.error;
        readModelEntityStore.setPmaQueue(chatId, []);
      }
    });

    await Promise.all([timelineTask, progressTask, queueTask]);
    if (activeChatId !== chatId || refreshSeq !== activeRefreshSeq) return;
    if (missingThreadError) {
      activeError = missingThreadError;
      loadingActive = false;
      closeStream();
      return;
    }
    if (!options.quiet || loadingActive) loadingActive = false;
  }

  function connectStream(chatId: string): void {
    closeStream();
    streamState = 'connecting';
    streamError = null;
    refreshedTerminalTurnId = null;
    streamSubscription = openPmaTailEventSource(chatId, {
      onStatus: (status) => {
        if (activeChatId !== chatId) return;
        if (status === 'connecting' && streamState !== 'connected') streamState = 'connecting';
        if (status === 'connected') {
          streamState = 'connected';
          streamError = null;
        }
        if (status === 'interrupted') streamState = 'interrupted';
      },
      onEvent: (event) => {
        if (activeChatId !== chatId) return;
        streamState = 'connected';
        if (event.kind === 'timeline') {
          const item = mapPmaTimelineItem(event.payload);
          readModelEntityStore.replacePmaTimeline(chatId, reconcilePmaTimeline(currentTimeline(chatId), [item]));
          if (item.kind === 'user_message') dropOptimisticPlaceholders();
          return;
        }
        if (event.kind === 'tail') return;
        if (event.kind === 'progress' || event.kind === 'state') {
          const nextProgress = mapPmaRunProgress(event.payload);
          updateProgress(nextProgress);
          if (
            event.kind === 'progress' &&
            nextProgress.terminal &&
            nextProgress.id &&
            refreshedTerminalTurnId !== nextProgress.id
          ) {
            refreshedTerminalTurnId = nextProgress.id;
            scheduleActiveRefresh(chatId, 700);
          }
          // Only honor streamShouldClose from `progress` events. The `state`
          // frame is the initial snapshot; a closed-from-state would tear the
          // subscription down before any new turn can stream.
          if (event.kind === 'progress' && nextProgress.streamShouldClose) {
            closeStream();
            return;
          }
        }
        if (event.kind === 'message') {
          const payload = event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload)
            ? (event.payload as JsonRecord)
            : null;
          if (payload && payload.contract_version === 'managed_thread_timeline.v2') {
            const item = mapPmaTimelineItem(payload);
            readModelEntityStore.replacePmaTimeline(chatId, reconcilePmaTimeline(currentTimeline(chatId), [item]));
            if (item.kind === 'user_message') dropOptimisticPlaceholders();
          }
          scheduleActiveRefresh(chatId, 250);
          return;
        }
      },
      onError: () => {
        if (activeChatId !== chatId) return;
        if (progress?.streamShouldClose) {
          closeStream();
          return;
        }
        streamState = 'interrupted';
        streamError = 'Live chat updates were interrupted. Reconnecting and repairing from the latest snapshot.';
        scheduleActiveRefresh(chatId, 900);
      }
    });
  }

  function scheduleActiveRefresh(chatId: string, delayMs = 600): void {
    if (pendingRefreshTimer) window.clearTimeout(pendingRefreshTimer);
    pendingRefreshTimer = window.setTimeout(() => {
      pendingRefreshTimer = null;
      if (activeChatId === chatId) void refreshActive(chatId, { quiet: true });
    }, delayMs);
  }

  function closeStream(): void {
    streamSubscription?.close();
    streamSubscription = null;
    streamState = 'idle';
  }

  function isMissingManagedThreadError(error: ApiError): boolean {
    return error.status === 404 && error.message.toLowerCase().includes('managed thread not found');
  }

  function upsertPmaChats(nextChats: PmaChatSummary[]): void {
    readModelEntityStore.upsertChatIndexRows(nextChats.map(pmaChatSummaryToChatIndexRow));
  }

  /** Elapsed seconds capped by wall clock while status is running (matches live UI). */
  function progressElapsedWithLiveWall(value: PmaRunProgress, nowMs: number): number {
    const base = value.elapsedSeconds ?? 0;
    if (value.status !== 'running' || !value.startedAt) return base;
    const startedMs = Date.parse(value.startedAt);
    if (!Number.isFinite(startedMs)) return base;
    const wallElapsed = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
    return Math.max(base, wallElapsed);
  }

  function updateProgress(nextProgress: PmaRunProgress): void {
    syncChatListStatusFromProgress(nextProgress);
    const chatId = nextProgress.chatId ?? activeChatId;
    if (!chatId) return;
    const nowMs = Date.now();
    const previousProgress = currentProgress(chatId);
    if (previousProgress && previousProgress.id === nextProgress.id) {
      const incomingElapsed = nextProgress.elapsedSeconds ?? 0;
      const mergedElapsed = Math.max(progressElapsedWithLiveWall(previousProgress, nowMs), incomingElapsed);
      const seen = new Set<string>();
      const merged: SurfaceArtifact[] = [];
      for (const ev of [...previousProgress.events, ...nextProgress.events]) {
        if (!ev.id || seen.has(ev.id)) continue;
        seen.add(ev.id);
        merged.push(ev);
      }
      readModelEntityStore.setPmaProgress(chatId, {
        ...nextProgress,
        startedAt: nextProgress.startedAt ?? previousProgress.startedAt,
        elapsedSeconds: mergedElapsed,
        events: merged
      });
    } else {
      readModelEntityStore.setPmaProgress(chatId, nextProgress);
    }
  }

  function progressWithLiveElapsed(value: PmaRunProgress | null, nowMs: number): PmaRunProgress | null {
    if (!value) return value;
    const elapsedSeconds = progressElapsedWithLiveWall(value, nowMs);
    return elapsedSeconds === value.elapsedSeconds ? value : { ...value, elapsedSeconds };
  }

  function hasCachedDetail(chatId: string): boolean {
    const state = readModelEntityStore.snapshot();
    return Boolean(
      state.pmaTimelines[chatId]?.order.length ||
      state.pmaProgress[chatId] ||
      state.pmaQueues[chatId]?.length ||
      state.timelines[chatId]?.order.length ||
      state.chatDetails[chatId]?.thread
    );
  }

  function activeDetailLoadResult(chatId: string): ReadModelLoaderResult | null {
    const data = safePageData();
    if (data?.chatId !== chatId) return null;
    return data.activeDetail ?? null;
  }

  function chatIndexLoadError(): ApiError | null {
    const data = safePageData();
    return data?.chatIndex?.status === 'error' ? data.chatIndex.error : null;
  }

  function safePageData(): ChatRouteLoadData | undefined {
    try {
      return page.data as ChatRouteLoadData | undefined;
    } catch {
      return undefined;
    }
  }

  function hasChatIndexProjection(state: typeof readModelState): boolean {
    return Boolean(state.chatIndexCursor || state.chatOrder.length > 0);
  }

  function currentTimeline(chatId: string): PmaTimelineItem[] {
    return selectPmaTimeline(readModelEntityStore.snapshot(), chatId);
  }

  function currentProgress(chatId: string): PmaRunProgress | null {
    return selectPmaProgress(readModelEntityStore.snapshot(), chatId);
  }

  function syncChatListStatusFromProgress(nextProgress: PmaRunProgress): void {
    const chatId = nextProgress.chatId ?? activeChatId;
    if (!chatId) return;
    const chat = chats.find((item) => item.id === chatId);
    if (!chat) return;
    upsertPmaChats([
      {
        ...chat,
        status: nextProgress.status,
        progressPercent: nextProgress.progressPercent ?? chat.progressPercent,
        updatedAt:
          nextProgress.lastEventAt && (!chat.updatedAt || nextProgress.lastEventAt > chat.updatedAt)
            ? nextProgress.lastEventAt
            : chat.updatedAt,
        raw: {
          ...chat.raw,
          execution_status: nextProgress.status,
          normalized_status: nextProgress.status,
          status: nextProgress.status,
          active_turn_id: nextProgress.id,
          queued_count: nextProgress.queueDepth
        }
      }
    ]);
  }

  function dropOptimisticPlaceholders(): void {
    if (activeChatId) readModelEntityStore.removeOptimisticPmaTimelineItems(activeChatId);
  }

  function retryStream(): void {
    if (!activeChatId) return;
    if (isLocalDraftChatId(activeChatId)) return;
    connectStream(activeChatId);
    void refreshActive(activeChatId, { quiet: true });
  }

  function syncSelectorsToActiveChat(): void {
    const chat = chats.find((item) => item.id === activeChatId);
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
      void loadModels(selectedAgent);
      return;
    }
    const previousAgent = selectedAgent;
    selectedAgent = resolved.agentId;
    selectedProfile = resolved.agentProfile;
    selectedReasoning = resolved.reasoning;
    if (previousAgent !== resolved.agentId || models.length === 0) {
      void loadModels(resolved.agentId, resolved.model ?? selectedModel);
    } else if (resolved.model) {
      selectedModel = resolved.model;
    }
  }

  function handleAgentChange(): void {
    if (selectedAgent !== 'hermes') selectedProfile = '';
    void loadModels(selectedAgent);
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
      id: `draft:pma:${Date.now()}`,
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
        scope_urn: scope.scopeUrn,
        chat_kind: chatKind
      }
    };
  }

  function isLocalDraftChatId(chatId: string | null): boolean {
    return Boolean(chatId && chatId.startsWith('draft:pma:'));
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
    return distanceFromBottom < 80;
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

  async function createChat(): Promise<void> {
    creating = true;
    composeError = null;
    localDraftChat = newDraftChatSummary();
    activeChatId = localDraftChat.id;
    detailMode = 'detail';
    newChatKind = 'pma';
    closeStream();
    void goto(href('/chats'), { replaceState: true });
    creating = false;
  }

  async function sendMessage(busyPolicy: 'queue' | 'interrupt' | null = null): Promise<void> {
    if ((!draft.trim() && pendingAttachments.length === 0) || !activeChatId) return;
    const draftSnapshot = draft;
    const attachmentsSnapshot = pendingAttachments;
    const optimisticChatId = activeChatId;
    const optimisticId = `optimistic:user:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
    const optimisticTimestamp = new Date().toISOString();
    const optimisticPlaceholder: PmaTimelineItem = {
      id: optimisticId,
      kind: 'user_message',
      orderKey: `optimistic|${optimisticTimestamp}|${optimisticId}`,
      timestamp: optimisticTimestamp,
      chatId: optimisticChatId,
      turnId: '',
      status: null,
      payload: {
        text: draftSnapshot,
        text_preview: draftSnapshot.slice(0, 240),
        attachments: attachmentsSnapshot.map((att) => ({
          id: att.id,
          kind: att.kind,
          title: att.title,
          url: att.url,
          size_label: att.sizeLabel
        }))
      },
      ...pmaTimelineContractFields(optimisticId, { correlationId: optimisticId }),
      raw: { optimistic: true }
    };
    readModelEntityStore.optimisticSend(
      optimisticChatId,
      {
        itemId: optimisticId,
        kind: 'user_message',
        role: 'user',
        createdAt: optimisticTimestamp,
        text: draftSnapshot,
        artifactIds: [],
        clientMessageId: optimisticId
      },
      optimisticId
    );
    readModelEntityStore.replacePmaTimeline(
      optimisticChatId,
      reconcilePmaTimeline(currentTimeline(optimisticChatId), [optimisticPlaceholder])
    );
    draft = '';
    pendingAttachments = [];
    const composerVersionAtClear = composerEditVersion;
    sending = true;
    composeError = null;

    const removeOptimistic = () => {
      readModelEntityStore.removeOptimisticPmaTimelineItems(optimisticChatId);
    };
    const restoreDraft = () => {
      removeOptimistic();
      readModelEntityStore.failOptimisticMutation(optimisticId);
      if (composerEditVersion !== composerVersionAtClear) return;
      draft = draftSnapshot;
      pendingAttachments = attachmentsSnapshot;
    };

    const uploaded = await ensureAttachmentsUploaded(attachmentsSnapshot);
    if (!uploaded) {
      restoreDraft();
      sending = false;
      return;
    }
    const attachmentsForMessage = uploaded;
    const message = composeMessageWithAttachments(draftSnapshot, attachmentsForMessage);
    const targetChatId = activeChatId;
    const targetIsDraft = isLocalDraftChatId(targetChatId);
    const targetIsRunning = displayedProgress?.status === 'running';
    const profileForSend =
      activeChat?.agentProfile?.trim() || selectedProfile?.trim() || '';
    const commandPlan =
      targetIsDraft
        ? planStartAndSendChat(
            selectedScope,
            selectedAgent,
            selectedProfile,
            selectedModel,
            message,
            {
              name: activeChat?.title || newChatDisplayName(),
              chatKind: newChatKind === 'agent' && canStartCodingAgentChat ? 'coding_agent' : 'pma',
              attachments: attachmentsForMessage,
              reasoning: selectedReasoning,
              clientTurnId: optimisticId
            }
          )
        : busyPolicy === 'interrupt'
        ? planInterruptExistingChat(targetChatId, message, {
            model: selectedModel,
            attachments: attachmentsForMessage,
            reasoning: selectedReasoning,
            profile: profileForSend,
            clientTurnId: optimisticId
          })
        : busyPolicy === 'queue' || targetIsRunning
          ? planQueueExistingChat(targetChatId, message, {
              model: selectedModel,
              attachments: attachmentsForMessage,
              reasoning: selectedReasoning,
              profile: profileForSend,
              clientTurnId: optimisticId
            })
          : planSendExistingChat(targetChatId, message, {
              model: selectedModel,
              attachments: attachmentsForMessage,
              reasoning: selectedReasoning,
              profile: profileForSend,
              clientTurnId: optimisticId
            });
    const result = await executePmaChatCommandPlan(pmaApi, commandPlan);
    if (result.ok) {
      const committedChatId = targetIsDraft ? result.data.chatId : targetChatId;
      if (!committedChatId) {
        removeOptimistic();
        composeError = {
          kind: 'parse',
          status: null,
          code: 'missing_chat_id',
          message: 'Started chat response did not include a managed thread id.'
        };
        sending = false;
        return;
      }
      if (targetIsDraft) {
        localDraftChat = null;
        activeChatId = committedChatId;
        detailMode = 'detail';
        connectStream(committedChatId);
        // Must complete before `sending` clears: the URL effect treats no `chatId`
        // segment as "list mode" unless we're still sending or on a local draft id.
        await syncDetailUrl(committedChatId);
      }
      await invalidateChatMutation(committedChatId);
      const optimisticFromBackend = optimisticUserTimelineItemFromSend(
        result.data.raw,
        draftSnapshot,
        committedChatId
      );
      if (optimisticFromBackend) {
        readModelEntityStore.replacePmaTimeline(
          committedChatId,
          reconcilePmaTimeline(currentTimeline(committedChatId), [optimisticFromBackend])
        );
        readModelEntityStore.reconcileOptimisticTimelineItem(committedChatId, optimisticId, {
          itemId: optimisticFromBackend.id,
          kind: 'user_message',
          role: 'user',
          createdAt: optimisticFromBackend.timestamp ?? optimisticTimestamp,
          text: draftSnapshot,
          artifactIds: [],
          clientMessageId: optimisticId,
          backendMessageId: optimisticFromBackend.turnId || undefined
        });
      }
      await refreshActive(committedChatId, { quiet: true });
      removeOptimistic();
      if (activeChat?.agentId === 'hermes' && profileForSend.trim()) {
        const stamped = profileForSend.trim();
        const chat = chats.find((row) => row.id === committedChatId);
        if (chat) upsertPmaChats([{ ...chat, agentProfile: stamped }]);
      }
    } else {
      restoreDraft();
      composeError = result.error;
    }
    sending = false;
  }

  async function interruptWithDraft(): Promise<void> {
    if (!canInterruptWithDraft) return;
    await sendMessage('interrupt');
  }

  async function cancelQueuedTurn(turn: PmaQueuedTurn, options: { confirmed?: boolean } = {}): Promise<void> {
    if (!activeChatId || !turn.managedTurnId) return;
    if (!options.confirmed) {
      const ok = await confirmDialog({
        title: 'Cancel queued message',
        message: `Cancel queued message ${turn.position}?`,
        confirmText: 'Cancel message',
        danger: true
      });
      if (!ok) return;
    }
    composeError = null;
    const result = await pmaApi.pma.cancelQueuedTurn(activeChatId, turn.managedTurnId);
    if (result.ok) {
      readModelEntityStore.setPmaQueue(
        activeChatId,
        queuedTurns.filter((item) => item.managedTurnId !== turn.managedTurnId)
      );
      await refreshActive(activeChatId, { quiet: true });
    } else {
      composeError = result.error;
    }
  }

  async function interruptWithQueuedTurn(turn: PmaQueuedTurn): Promise<void> {
    if (!activeChatId || !turn.prompt.trim()) return;
    const chatId = activeChatId;
    composeError = null;
    const cancelResult = await pmaApi.pma.cancelQueuedTurn(chatId, turn.managedTurnId);
    if (!cancelResult.ok) {
      composeError = cancelResult.error;
      return;
    }
    readModelEntityStore.setPmaQueue(
      chatId,
      queuedTurns.filter((item) => item.managedTurnId !== turn.managedTurnId)
    );
    const profileForSend =
      activeChat?.agentProfile?.trim() || selectedProfile?.trim() || '';
    const result = await executePmaChatCommandPlan(
      pmaApi,
      planInterruptExistingChat(chatId, turn.prompt, {
        model: turn.model ?? selectedModel,
        attachments: turn.attachments as DocumentFileIntentPayload[],
        reasoning: turn.reasoning ?? selectedReasoning,
        profile: profileForSend
      })
    );
    if (result.ok) {
      await invalidateChatMutation(chatId);
      const optimisticFromBackend = optimisticUserTimelineItemFromSend(
        result.data.raw,
        turn.prompt,
        chatId
      );
      if (optimisticFromBackend) {
        readModelEntityStore.replacePmaTimeline(
          chatId,
          reconcilePmaTimeline(currentTimeline(chatId), [optimisticFromBackend])
        );
      }
      await refreshActive(chatId, { quiet: true });
    } else {
      composeError = result.error;
    }
  }

  async function autoCompactActiveThread(chatId: string): Promise<void> {
    if (displayedProgress?.status === 'running') {
      showCommandNotice('Wait for the current turn to finish before auto-compacting.');
      return;
    }
    sending = true;
    showCommandNotice('Generating compact summary...');
    const summaryResult = await pmaApi.pma.sendMessage(
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
    const compactResult = await pmaApi.pma.compactThread(chatId, summary);
    if (!compactResult.ok) {
      composeError = compactResult.error;
      sending = false;
      return;
    }
    await invalidateChatMutation(chatId);
    upsertPmaChats([compactResult.data]);
    showCommandNotice('Compact summary generated and saved.');
    sending = false;
    await refreshActive(chatId, { quiet: true });
    clearSlashDraft();
  }

  function clearSlashDraft(): void {
    draft = '';
    markComposerEdited();
    queueMicrotask(() => autosizeComposer());
  }

  function applySlashCommand(spec: SlashCommandSpec): void {
    draft = `/${spec.name}${spec.id === 'compact' || spec.id === 'cancel' || spec.id === 'agent' || spec.id === 'model' || spec.id === 'reasoning' || spec.id === 'profile' || spec.id === 'new' ? ' ' : ''}`;
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
      await createChat();
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'newt') {
      newChatKind = 'agent';
      await createChat();
      showCommandNotice('Started a fresh coding chat.');
      clearSlashDraft();
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
      await createChat();
      showCommandNotice('Started a fresh replacement chat.');
      clearSlashDraft();
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
      const result = await pmaApi.pma.listFiles();
      if (!result.ok) composeError = result.error;
      else {
        readModelEntityStore.setPmaArtifacts(activeChatId ?? '__global__', result.data);
        showCommandNotice(result.data.length ? `Files refreshed (${result.data.length}).` : 'No PMA files yet.');
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'interrupt') {
      const result = await pmaApi.pma.interruptThread(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        showCommandNotice('Interrupt requested.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'resume') {
      const result = await pmaApi.pma.resumeThread(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        await invalidateChatMutation(activeChatId);
        upsertPmaChats([result.data]);
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
      const result = await pmaApi.pma.compactThread(activeChatId, args);
      if (!result.ok) composeError = result.error;
      else {
        await invalidateChatMutation(activeChatId);
        upsertPmaChats([result.data]);
        showCommandNotice('Compaction seed saved.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'archive') {
      const archivedId = activeChatId;
      await archiveChat(archivedId, { confirmed: true });
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
      const result = await pmaApi.pma.clearQueue(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        readModelEntityStore.setPmaQueue(activeChatId, []);
        showCommandNotice('Queue cleared.');
        await refreshActive(activeChatId, { quiet: true });
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
    pendingAttachments = [...pendingAttachments, ...next];
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
    pendingAttachments = [
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
    ];
    markComposerEdited();
    cancelLinkDialog();
  }

  function removeAttachment(attachmentId: string): void {
    pendingAttachments = removePendingAttachment(pendingAttachments, attachmentId);
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
    markComposerEdited();
    autosizeComposer();
  }

  async function ensureAttachmentsUploaded(attachments: PendingAttachment[]): Promise<PendingAttachment[] | null> {
    const uploaded: PendingAttachment[] = [];
    for (const attachment of attachments) {
      const file = (attachment as PendingAttachment & { file?: File }).file;
      if (!file || attachment.uploadedName) {
        uploaded.push(attachment);
        continue;
      }
      const result = await pmaApi.pma.uploadInboxFile(file);
      if (!result.ok || !result.data[0]) {
        composeError = result.ok
          ? { kind: 'parse', status: null, code: 'upload_missing_file', message: 'Upload did not return a file name.' }
          : result.error;
        return null;
      }
      const uploadedName = result.data[0];
      uploaded.push({
        ...attachment,
        uploadedName,
        url: `/hub/pma/files/inbox/${encodeURIComponent(uploadedName)}`,
        uploadState: 'uploaded'
      });
    }
    return uploaded;
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
      <button class="new-chat-button" type="button" onclick={createChat} disabled={creating}>
        {createChatLabel}
      </button>
    </div>

    <div class="chat-filter-bar">
      <FilterRow
        rootClass="chat-filter-chips-row"
        ariaLabel="Chat filters"
        items={[
          ...PMA_CHAT_FILTER_ORDER.filter(
            (item) => item !== 'unread' || filterCounts.unread > 0 || filter === 'unread'
          ).map((item) => ({
            key: `status:${item}`,
            label: filterChipLabel(item),
            count: filterCounts[item],
            active: filter === item,
            onSelect: () => (filter = item)
          })),
          ...(ticketRunGroupCount > 0 || filter === PMA_CHAT_TICKET_RUNS_FILTER
            ? [
                {
                  key: 'ticket-runs',
                  label: 'Ticket Runs',
                  count: ticketRunGroupCount,
                  active: filter === PMA_CHAT_TICKET_RUNS_FILTER,
                  onSelect: () => (filter = PMA_CHAT_TICKET_RUNS_FILTER)
                }
              ]
            : []),
          ...surfaceFilterChips
            .filter((surf) => surf.count > 0 || filter === pmaChatSurfaceFilterToken(surf.slug))
            // Suppress the surface chip when it is the only surface available and its
            // count duplicates the All count — the chip would communicate nothing new
            // and forces the filter row onto a second line.
            .filter((surf, _idx, arr) => {
              if (filter === pmaChatSurfaceFilterToken(surf.slug)) return true;
              if (arr.length !== 1) return true;
              return surf.count !== filterCounts.all;
            })
            .map((surf) => ({
              key: `surface:${surf.slug}`,
              label: surf.label,
              count: surf.count,
              active: filter === pmaChatSurfaceFilterToken(surf.slug),
              onSelect: () => (filter = pmaChatSurfaceFilterToken(surf.slug))
            }))
        ]}
      />
      {#if filter === 'unread' && filterCounts.unread > 0}
        <button
          class="new-chat-button"
          type="button"
          onclick={markAllUnreadChatsRead}
          aria-label="Mark all chats as read"
        >
          Mark all as read
        </button>
      {/if}
      {#if activeChatCount > 0 && filter !== 'archived'}
        <button
          class="new-chat-button archive-all-button"
          type="button"
          onclick={archiveAllActiveChats}
          disabled={archiving}
          aria-label="Archive all active chats"
        >
          {archiving ? 'Archiving...' : 'Archive All'}
        </button>
      {/if}
    </div>

    {#snippet chatRow(chat: import('$lib/viewModels/domain').PmaChatSummary, nested: boolean)}
      {@const scopeTags = pmaChatScopeTagView(chat, {
        repoLabel: repoLabelForRepoId,
        worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null
      })}
      {@const messengerSurface = pmaChatMessengerSurface(chat)}
      {@const listScopeAccent = chatListScopeAccentLabel(chat, scopeTags)}
      {@const listScopeAccentHex = listScopeAccent ? repoAccent(listScopeAccent) : null}
      {@const listAgentLabel = agentDisplayForChat(agents, chat)}
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
          <span class="chat-title-row">
            <span class="chat-title-cluster">
              {#if isChatUnread(chat, lastSeenMap)}
                <span class="chat-unread-dot" aria-label="Unread"></span>
              {/if}
              <span class="chat-title-text-badge">
                <strong>{nested && chat.ticketId ? chat.ticketId : chat.title}</strong>
                {#if !nested}
                  <span class={`chat-scope-kind-tag ${scopeTags.kindKey}`}>{scopeTags.kindLabel}</span>
                {/if}
                {#if isPmaChatArchived(chat)}
                  <span class="chat-scope-kind-tag archived">Archived</span>
                {/if}
                {#if pmaChatKind(chat) === 'coding_agent'}
                  <span class={`chat-kind-badge ${pmaChatKind(chat)}`}>{pmaChatKindLabel(pmaChatKind(chat))}</span>
                {/if}
                {#if messengerSurface}
                  <span class={`chat-surface-badge ${messengerSurface.badgeClass}`}>{messengerSurface.label}</span>
                {/if}
              </span>
            </span>
            <span class="chat-title-trailing">
              {#if chat.status !== 'idle' && chat.status !== 'done'}
                <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
              {/if}
              {#if chat.updatedAt}
                <span class="updated-at">{formatRelativeTime(chat.updatedAt)}</span>
              {/if}
            </span>
          </span>
          <span class="chat-meta-row">
            {#if !nested}
              <span class="chat-scope-tags">
                <span
                  class="chat-scope-detail-tag"
                  style={listScopeAccentHex ? `color: ${listScopeAccentHex}` : undefined}
                  title={scopeTags.detailFull ?? scopeTags.detail}
                >{scopeTags.detail}</span>
              </span>
              {#if chat.ticketId}
                <span class="chat-meta-dot" aria-hidden="true">·</span>
                <code>{chat.ticketId}</code>
              {/if}
            {:else if chat.title && chat.title !== chat.ticketId}
              <span class="chat-nested-title" title={chat.title}>{chat.title}</span>
            {/if}
            {#if listAgentLabel || chat.model}
              {#if !nested || (chat.title && chat.title !== chat.ticketId)}
                <span class="chat-meta-dot" aria-hidden="true">·</span>
              {/if}
              <span class="chat-agent-model">
                {#if listAgentLabel}<span class="chat-agent">{listAgentLabel}</span>{/if}
                {#if listAgentLabel && chat.model}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                {#if chat.model}<span class="chat-model">{chat.model}</span>{/if}
              </span>
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
          <button type="button" onclick={() => void chatIndexSession.refresh()}>Retry</button>
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
                      class="chat-run-mark-read"
                      onclick={(event) => { event.stopPropagation(); markGroupRead(group); }}
                    >Mark run as read</button>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        {/snippet}
        </VirtualList>
        {#if filteredEntries.length === 0}
          <div class="state-panel empty-state compact-empty chat-filter-empty">
            <strong>No chats match this filter</strong>
            <p>Clear the search or try another filter.</p>
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
            <span class={`chat-kind-badge ${activeChatKind}`}>{activeChatKindLabel}</span>
            {#if activeMessengerSurface}
              <span class={`chat-surface-badge ${activeMessengerSurface.badgeClass}`}>{activeMessengerSurface.label}</span>
            {/if}
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
              <span class="chat-header-config" title="Locked for this chat">
                {selectedAgentLabel}{selectedModelLabel ? ` · ${selectedModelLabel}` : ''}{showEffortSelector ? ` · ${selectedEffortLabel}` : ''}
              </span>
            {/if}
          </p>
        {/if}
      </div>
      {#if activeChat && (showStreamHealthAside || !isPmaChatArchived(activeChat))}
        <div class="chat-header-tools">
          {#if !isPmaChatArchived(activeChat)}
            <button
              class="chat-header-action"
              type="button"
              onclick={() => archiveChat(activeChat.id)}
              disabled={archiving}
              aria-label="Archive this chat"
            >
              {archiving ? 'Archiving...' : 'Archive'}
            </button>
          {/if}
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
                  <button type="button" onclick={retryStream}>Reconnect</button>
                {/if}
              </div>
            </aside>
          {/if}
        </div>
      {/if}
    </div>

    <div bind:this={messageStack} class="message-stack" aria-live="off">
      {#if loadingActive && (activeChat || activeChatId)}
        <div class="state-panel loading-state">
          <span class="state-icon" aria-hidden="true"></span>
          <strong>Loading active chat</strong>
          <p>Collecting timeline and status.</p>
        </div>
      {:else if activeError}
        <div class="state-panel error">
          <strong>Could not load this chat</strong>
          <p>{activeError.message}</p>
          <button type="button" onclick={() => activeChatId && refreshActive(activeChatId)}>Retry</button>
        </div>
      {:else if !activeChat}
        <div class="state-panel empty-state">
          <strong>No chat is selected</strong>
          <p>Pick a conversation or create a new chat to start work.</p>
          <button type="button" onclick={() => (detailMode = 'list')}>Browse chats</button>
        </div>
      {:else if showStartPicker}
        <div class="start-picker" aria-label="Start of chat configuration">
          <ChatThreadPreMessagePickers
            {agents}
            bind:agentValue={selectedAgent}
            bind:profileValue={selectedProfile}
            bind:modelValue={selectedModel}
            bind:reasoningValue={selectedReasoning}
            {models}
            loading={loadingModels}
            showAgent={showAgentSelector}
            onAgentChange={handleAgentChange}
            onPickerChange={handlePickerChange}
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
        <ChatTranscriptCards cards={activeCards} assistantLabel={chatAgentDisplayLabel} {streamingMessageId} />
        {#if showTypingIndicator}
          {@render typingDots('Assistant is typing')}
        {/if}
      {/if}
    </div>
    <div class="sr-only" aria-live="polite" aria-atomic="false">{srAnnouncement}</div>

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
        <div class="queued-message-list" aria-label="Queued messages">
          {#each queuedTurns as turn (turn.managedTurnId)}
            <div class="queued-message-row">
              <span class="queued-message-position">#{turn.position}</span>
              <span class="queued-message-copy" title={turn.prompt}>{turn.promptPreview || turn.prompt}</span>
              <span class="queued-message-state">{turn.state || 'queued'}</span>
              <button
                class="queued-message-action"
                type="button"
                aria-label={`Interrupt with queued message ${turn.position}`}
                title="Interrupt with this queued message"
                onclick={() => interruptWithQueuedTurn(turn)}
              >
                Interrupt
              </button>
              <button
                class="queued-message-action subtle"
                type="button"
                aria-label={`Cancel queued message ${turn.position}`}
                title="Cancel queued message"
                onclick={() => cancelQueuedTurn(turn)}
              >
                Cancel
              </button>
            </div>
          {/each}
        </div>
      {/if}
      {#if pendingAttachments.length > 0}
        <div class="pending-attachments" aria-label="Pending attachments">
          {#each pendingAttachments as attachment (attachment.id)}
            <span class={`pending-attachment ${attachment.uploadState}`}>
              <span>{attachment.kind}</span>
              <strong>{attachment.title}</strong>
              {#if attachment.sizeLabel}<em>{attachment.sizeLabel}</em>{/if}
              <button type="button" aria-label={`Remove ${attachment.title}`} onclick={() => removeAttachment(attachment.id)}>x</button>
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
        {sending ? 'Queueing' : displayedProgress?.status === 'running' ? 'Queue' : 'Send'}
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
            <button type="button" class="secondary-link" onclick={cancelLinkDialog}>Cancel</button>
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
