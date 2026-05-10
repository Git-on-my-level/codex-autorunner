<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount, tick } from 'svelte';
  import MasterDetail from '$lib/components/MasterDetail.svelte';
  import ChatTranscriptCards from '$lib/components/ChatTranscriptCards.svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import ChatThreadPreMessagePickers from '$lib/components/ChatThreadPreMessagePickers.svelte';
  import VoiceComposerButton from '$lib/components/VoiceComposerButton.svelte';
  import { pmaApi, type ApiError, type JsonRecord, type PmaQueuedTurn } from '$lib/api/client';
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
  import { mapPmaRunProgress, mapPmaTimelineItem } from '$lib/viewModels/domain';
  import type {
    PmaChatSummary,
    PmaRunProgress,
    PmaTimelineItem,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    buildManagedThreadCreatePayload,
    buildPmaChatListEntries,
    buildPmaChatScopeOptions,
    buildManagedThreadMessagePayload,
    buildPmaTranscriptCards,
    buildPmaStatusBar,
    chooseActiveChatId,
    composeMessageWithAttachments,
    countTicketRunGroups,
    filterPmaChatEntries,
    formatBytes,
    formatRelativeTime,
    localPmaChatScopeOption,
    PMA_CHAT_FILTER_ORDER,
    PMA_CHAT_TICKET_RUNS_FILTER,
    pmaChatKind,
    pmaChatKindLabel,
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
    stringField
  } from '$lib/viewModels/modelPickers';
  import {
    buildSlashCommandSuggestions,
    parseSlashCommand,
    WEB_SLASH_COMMANDS,
    type SlashCommandSpec,
    type SlashCommandSuggestion
  } from '$lib/viewModels/slashCommands';

  const COMPACT_SUMMARY_PROMPT =
    'Summarize the conversation so far into a concise context block I can paste into a new thread. Include goals, constraints, decisions, and current state.';

  let chats = $state<PmaChatSummary[]>([]);
  let timeline = $state<PmaTimelineItem[]>([]);
  let progress = $state<PmaRunProgress | null>(null);
  let artifacts = $state<SurfaceArtifact[]>([]);
  let queuedTurns = $state<PmaQueuedTurn[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let scopeOptions = $state<PmaChatScopeOption[]>(buildPmaChatScopeOptions([], []));
  let pendingAttachments = $state<PendingAttachment[]>([]);
  let linkDialogOpen = $state(false);
  let linkDraft = $state('');
  let activeChatId = $state<string | null>(null);
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
  let loadingModels = $state(false);
  let chatError = $state<ApiError | null>(null);
  let activeError = $state<ApiError | null>(null);
  let composeError = $state<ApiError | null>(null);
  let streamState = $state<'idle' | 'connecting' | 'connected' | 'interrupted'>('idle');
  let streamError = $state<string | null>(null);
  let streamSubscription: StreamSubscription | null = null;
  let fileInput: HTMLInputElement | null = $state(null);
  let imageInput: HTMLInputElement | null = $state(null);
  let messageStack: HTMLDivElement | null = $state(null);
  let composerTextarea: HTMLTextAreaElement | null = $state(null);
  let voiceNotice = $state<string | null>(null);
  let commandNotice = $state<string | null>(null);
  let slashSelectedIndex = $state(0);
  let composerFocused = $state(false);
  let composerEditVersion = 0;

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
  let lastScrolledChatId: string | null = null;
  let lastScrolledCardCount = 0;
  let lastScrolledEventCount = 0;
  let lastSeenMap = $state<ChatLastSeenMap>(loadLastSeenMap());

  const activeChat = $derived(
    activeChatId
      ? chats.find((chat) => chat.id === activeChatId) ?? null
      : null
  );
  let expandedRunGroups = $state<Record<string, boolean>>({});
  const chatListEntries = $derived(
    buildPmaChatListEntries(chats, {
      lastSeen: lastSeenMap,
      repoLabel: repoLabelForRepoId,
      worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null,
      groupRuns: true
    })
  );
  const filteredEntries = $derived(filterPmaChatEntries(chatListEntries, filter, search, lastSeenMap));
  const filterCounts = $derived(summarizeFilterCounts(chats, lastSeenMap));
  const surfaceFilterChips = $derived(pmaChatSurfaceFilterOptions(chats));
  const ticketRunGroupCount = $derived(countTicketRunGroups(chats));

  function isGroupExpanded(group: PmaChatRunGroup): boolean {
    if (group.key in expandedRunGroups) return expandedRunGroups[group.key];
    // Default collapsed; expand only when the active chat lives inside this group.
    if (activeChatId && group.chats.some((chat) => chat.id === activeChatId)) return true;
    return false;
  }

  function toggleGroup(group: PmaChatRunGroup): void {
    expandedRunGroups = { ...expandedRunGroups, [group.key]: !isGroupExpanded(group) };
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
    lastSeenMap = next;
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
  const activeCards = $derived<PmaCard[]>(buildPmaTranscriptCards(timeline, activeChat, artifacts, progress));
  const statusBar = $derived(buildPmaStatusBar(progress, activeChat));
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
  const chatAgentDisplayLabel = $derived.by(() => {
    if (!activeChat) return 'Assistant';
    const fromCatalog = agentDisplayForChat(agents, activeChat);
    if (fromCatalog) return fromCatalog;
    const rawId = activeChat.agentId?.trim();
    if (rawId) return rawId;
    return activeChatKindLabel;
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
          : (progress?.elapsedSeconds !== null && progress?.elapsedSeconds !== undefined) ||
            (progress?.queueDepth ?? 0) > 0 ||
            statusBar.tokenUsageLabel ||
            statusBar.contextRemainingLabel ||
            ['running', 'waiting', 'blocked', 'failed'].includes(statusBar.state))
    )
  );
  const chatHasActivity = $derived(activeCards.length > 0 || showStatusBar);
  const showStartPicker = $derived(Boolean(activeChat) && !loadingActive && !activeError && !chatHasActivity);
  const hasRunnableDraft = $derived(Boolean(activeChat && (draft.trim() || pendingAttachments.length > 0)));
  const canInterruptWithDraft = $derived(Boolean(activeChat && progress?.status === 'running' && hasRunnableDraft));
  const slashSuggestions = $derived<SlashCommandSuggestion[]>(
    buildSlashCommandSuggestions(draft, {
      hasActiveChat: Boolean(activeChat),
      hasScopedWorkspace: selectedScope.kind !== 'local',
      isRunning: progress?.status === 'running',
      queueDepth: queuedTurns.length || progress?.queueDepth || 0
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
    draft = page.url.searchParams.get('draft') ?? draft;
    void loadInitial();
    const interval = window.setInterval(() => {
      if (activeChatId) void refreshActive(activeChatId, { quiet: true });
    }, 7000);
    return () => window.clearInterval(interval);
  });

  $effect(() => {
    const requestedDetail = requestedDetailFromUrl();
    if (!requestedDetail) {
      if (activeChatId !== null) {
        closeStream();
        activeChatId = null;
        timeline = [];
        progress = null;
        queuedTurns = [];
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
    lastSeenMap = next;
    saveLastSeenMap(next);
  });

  onDestroy(() => {
    if (pendingRefreshTimer) window.clearTimeout(pendingRefreshTimer);
    closeStream();
  });

  $effect(() => {
    const cardCount = activeCards.length;
    const eventCount = progress?.events.length ?? 0;
    const chatChanged = activeChatId !== lastScrolledChatId;
    const cardCountChanged = cardCount !== lastScrolledCardCount;
    const eventCountChanged = eventCount !== lastScrolledEventCount;

    if (!activeChat || loadingActive || (!chatChanged && !cardCountChanged && !eventCountChanged)) return;

    const shouldFollowLatest = chatChanged || isMessageStackNearBottom();
    lastScrolledChatId = activeChatId;
    lastScrolledCardCount = cardCount;
    lastScrolledEventCount = eventCount;
    if (shouldFollowLatest) void scrollMessagesToBottom();
  });

  async function loadInitial(): Promise<void> {
    loadingChats = true;
    chatError = null;
    const [chatResult, artifactResult, agentResult, repoResult, worktreeResult] = await Promise.all([
      pmaApi.pma.listChats(),
      pmaApi.pma.listFiles(),
      pmaApi.pma.listAgents(),
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);

    if (chatResult.ok) {
      chats = chatResult.data;
      const requestedChat = page.params.chatId ?? page.url.searchParams.get('chat');
      activeChatId = chooseActiveChatId(chatResult.data, activeChatId, requestedChat);
      if (activeChatId) {
        detailMode = 'detail';
        syncSelectorsToActiveChat();
        void refreshActive(activeChatId);
        connectStream(activeChatId);
      }
    } else {
      chatError = chatResult.error;
    }

    if (artifactResult.ok) artifacts = artifactResult.data;
    scopeOptions = buildPmaChatScopeOptions(
      repoResult.ok ? repoResult.data : [],
      worktreeResult.ok ? worktreeResult.data : []
    );
    if (!scopeOptions.some((scope) => scope.id === selectedScopeId)) selectedScopeId = 'local';
    if (!canStartCodingAgentChat) newChatKind = 'pma';
    if (agentResult.ok) {
      agents = agentResult.data.agents;
      const defaults = agentResult.data.defaults;
      const defaultProfile =
        typeof defaults.profile === 'string' && defaults.profile.trim() ? defaults.profile.trim() : '';
      if (!activeChat?.agentId) {
        selectedAgent = agentResult.data.agents[0] ? agentId(agentResult.data.agents[0]) : selectedAgent;
        if (selectedAgent === 'hermes' && defaultProfile && !selectedProfile.trim()) {
          selectedProfile = defaultProfile;
        }
      }
      void loadModels(selectedAgent, activeChat?.model ?? selectedModel);
    }
    applyNewChatQueryParam();
    loadingChats = false;
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

  async function loadModels(agentId: string, preferredModel = ''): Promise<void> {
    if (!agentCanListModels(agentRecordForId(agents, agentId))) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    loadingModels = true;
    const result = await pmaApi.pma.listAgentModels(agentId);
    if (!result.ok) {
      models = [];
      selectedModel = '';
      selectedReasoning = '';
      loadingModels = false;
      return;
    }
    models = result.data;
    selectedModel = preferredModel && modelExists(result.data, preferredModel)
      ? preferredModel
      : firstModelValue(result.data);
    if (selectedReasoning && !pickerReasoningOptions(result.data, selectedModel).includes(selectedReasoning)) {
      selectedReasoning = '';
    }
    loadingModels = false;
  }

  async function selectChat(chatId: string): Promise<void> {
    activeChatId = chatId;
    timeline = [];
    progress = null;
    queuedTurns = [];
    detailMode = 'detail';
    syncSelectorsToActiveChat();
    markActiveChatRead();
    await syncDetailUrl(chatId);
    await refreshActive(chatId);
    connectStream(chatId);
  }

  function requestedDetailFromUrl(): string | null {
    if (page.params.chatId) return page.params.chatId;
    const detail = page.url.searchParams.get('detail');
    if (detail?.startsWith('chat:')) return detail.slice('chat:'.length);
    return page.url.searchParams.get('chat');
  }

  async function activateDetailFromUrl(detailId: string): Promise<void> {
    if (detailId === activeChatId) return;
    if (!chats.some((chat) => chat.id === detailId)) return;
    await selectChatWithoutUrl(detailId);
  }

  async function selectChatWithoutUrl(chatId: string): Promise<void> {
    activeChatId = chatId;
    timeline = [];
    progress = null;
    queuedTurns = [];
    detailMode = 'detail';
    syncSelectorsToActiveChat();
    markActiveChatRead();
    await refreshActive(chatId);
    connectStream(chatId);
  }

  function markActiveChatRead(): void {
    if (!activeChatId) return;
    const chat = chats.find((c) => c.id === activeChatId);
    const stamp = chat?.updatedAt ?? new Date().toISOString();
    const next = markChatRead(lastSeenMap, activeChatId, stamp);
    if (next === lastSeenMap) return;
    lastSeenMap = next;
    saveLastSeenMap(next);
  }

  function markAllUnreadChatsRead(): void {
    const next = markAllChatsRead(lastSeenMap, chats);
    if (next === lastSeenMap) return;
    lastSeenMap = next;
    saveLastSeenMap(next);
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
    if (!options.quiet) {
      loadingActive = true;
      activeError = null;
    }
    const [messageResult, tailResult, statusResult, queueResult] = await Promise.all([
      pmaApi.pma.getTimeline(chatId),
      pmaApi.pma.getTail(chatId),
      pmaApi.pma.getStatus(chatId),
      pmaApi.pma.getQueue(chatId)
    ]);

    if (activeChatId !== chatId) return;
    if (messageResult.ok) timeline = reconcilePmaTimeline(timeline, messageResult.data);
    else if (!options.quiet) activeError = messageResult.error;

    if (tailResult.ok) updateProgress(tailResult.data);
    else if (statusResult.ok) updateProgress(statusResult.data);
    else if (!options.quiet) activeError = tailResult.error;

    if (queueResult.ok) queuedTurns = queueResult.data.queuedTurns;

    loadingActive = false;
  }

  function connectStream(chatId: string): void {
    closeStream();
    streamState = 'connecting';
    streamError = null;
    streamSubscription = openPmaTailEventSource(chatId, {
      onEvent: (event) => {
        if (activeChatId !== chatId) return;
        streamState = 'connected';
        if (event.kind === 'timeline') {
          const item = mapPmaTimelineItem(event.payload);
          timeline = reconcilePmaTimeline(timeline, [item]);
          if (item.kind === 'user_message') dropOptimisticPlaceholders();
          return;
        }
        if (event.kind === 'tail') return;
        if (event.kind === 'progress' || event.kind === 'state') {
          const nextProgress = mapPmaRunProgress(event.payload);
          updateProgress(nextProgress);
          if (shouldEndStream(event.kind, nextProgress)) {
            scheduleActiveRefresh(chatId, 700);
            closeStream();
            return;
          }
        }
        if (event.kind === 'message') {
          scheduleActiveRefresh(chatId, 250);
          return;
        }
        if (progress?.status === 'done' || progress?.status === 'failed') {
          scheduleActiveRefresh(chatId, 700);
        }
      },
      onError: () => {
        if (activeChatId !== chatId) return;
        if (progress && shouldEndStream('progress', progress)) {
          closeStream();
          return;
        }
        streamState = 'interrupted';
        streamError = 'Live chat updates were interrupted. Polling continues in the background.';
      }
    });
  }

  function shouldEndStream(kind: 'state' | 'progress', value: PmaRunProgress): boolean {
    return value.streamShouldClose || (kind === 'progress' && value.terminal);
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

  function updateProgress(nextProgress: PmaRunProgress): void {
    syncChatListStatusFromProgress(nextProgress);
    if (progress && progress.id === nextProgress.id) {
      const seen = new Set<string>();
      const merged: SurfaceArtifact[] = [];
      for (const ev of [...progress.events, ...nextProgress.events]) {
        if (!ev.id || seen.has(ev.id)) continue;
        seen.add(ev.id);
        merged.push(ev);
      }
      progress = { ...nextProgress, events: merged };
    } else {
      progress = nextProgress;
    }
  }

  function syncChatListStatusFromProgress(nextProgress: PmaRunProgress): void {
    const chatId = nextProgress.chatId ?? activeChatId;
    if (!chatId) return;
    chats = chats.map((chat) => {
      if (chat.id !== chatId) return chat;
      return {
        ...chat,
        status: nextProgress.status,
        progressPercent: nextProgress.progressPercent ?? chat.progressPercent,
        updatedAt: nextProgress.lastEventAt ?? chat.updatedAt,
        raw: {
          ...chat.raw,
          execution_status: nextProgress.status,
          normalized_status: nextProgress.status,
          status: nextProgress.status,
          active_turn_id: nextProgress.id,
          queued_count: nextProgress.queueDepth
        }
      };
    });
  }

  function dropOptimisticPlaceholders(): void {
    if (timeline.some((item) => item.id.startsWith('optimistic:'))) {
      timeline = timeline.filter((item) => !item.id.startsWith('optimistic:'));
    }
  }

  function retryStream(): void {
    if (!activeChatId) return;
    connectStream(activeChatId);
    void refreshActive(activeChatId, { quiet: true });
  }

  function syncSelectorsToActiveChat(): void {
    const chat = chats.find((item) => item.id === activeChatId);
    const scopeId = scopeIdForChat(chat);
    if (scopeId) selectedScopeId = scopeId;
    if (!chat?.agentId) return;
    selectedAgent = chat.agentId;
    selectedProfile = chat.agentProfile ?? '';
    selectedReasoning = stringField(chat.raw, 'reasoning') ?? '';
    void loadModels(chat.agentId, chat.model ?? selectedModel);
  }

  function handleAgentChange(): void {
    if (selectedAgent !== 'hermes') selectedProfile = '';
    void loadModels(selectedAgent);
  }

  function newChatDisplayName(): string {
    return newChatKind === 'agent' && canStartCodingAgentChat
      ? 'New coding agent chat'
      : 'New chat';
  }

  async function ensureChatForSelectedAgent(): Promise<string | null> {
    if (!activeChat?.agentId || activeChat.agentId === selectedAgent) return activeChatId;
    const result = await pmaApi.pma.createChat(
      buildManagedThreadCreatePayload(selectedAgent, selectedScope, newChatDisplayName(), selectedModel, selectedProfile)
    );
    if (!result.ok) {
      composeError = result.error;
      return null;
    }
    chats = [result.data, ...chats.filter((chat) => chat.id !== result.data.id)];
    await selectChat(result.data.id);
    return result.data.id;
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

  function isMessageStackNearBottom(): boolean {
    if (!messageStack) return true;
    const distanceFromBottom = messageStack.scrollHeight - messageStack.scrollTop - messageStack.clientHeight;
    return distanceFromBottom < 80;
  }

  async function scrollMessagesToBottom(): Promise<void> {
    await tick();
    if (!messageStack) return;
    messageStack.scrollTop = messageStack.scrollHeight;
  }

  async function createChat(): Promise<void> {
    creating = true;
    composeError = null;
    const result = await pmaApi.pma.createChat(
      buildManagedThreadCreatePayload(selectedAgent, selectedScope, newChatDisplayName(), selectedModel, selectedProfile)
    );
    if (result.ok) {
      chats = [result.data, ...chats.filter((chat) => chat.id !== result.data.id)];
      await selectChat(result.data.id);
      selectedScopeId = scopeIdForChat(result.data) ?? 'local';
      newChatKind = 'pma';
    } else {
      composeError = result.error;
    }
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
      raw: { optimistic: true }
    };
    timeline = reconcilePmaTimeline(timeline, [optimisticPlaceholder]);
    draft = '';
    pendingAttachments = [];
    const composerVersionAtClear = composerEditVersion;
    sending = true;
    composeError = null;

    const removeOptimistic = () => {
      timeline = timeline.filter((item) => !item.id.startsWith('optimistic:'));
    };
    const restoreDraft = () => {
      removeOptimistic();
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
    const targetChatId = await ensureChatForSelectedAgent();
    if (!targetChatId) {
      restoreDraft();
      sending = false;
      return;
    }
    const targetIsRunning = targetChatId === activeChatId && progress?.status === 'running';
    const resolvedBusyPolicy = busyPolicy ?? (targetIsRunning ? 'queue' : null);
    const result = await pmaApi.pma.sendMessage(
      targetChatId,
      buildManagedThreadMessagePayload(
        message,
        selectedModel,
        targetIsRunning,
        attachmentsForMessage,
        selectedReasoning,
        selectedProfile,
        resolvedBusyPolicy
      )
    );
    if (result.ok) {
      const optimisticFromBackend = optimisticUserTimelineItemFromSend(
        result.data.raw,
        draftSnapshot,
        targetChatId
      );
      if (optimisticFromBackend) timeline = reconcilePmaTimeline(timeline, [optimisticFromBackend]);
      await refreshActive(targetChatId, { quiet: true });
      removeOptimistic();
      if (selectedAgent === 'hermes' && selectedProfile.trim()) {
        const stamped = selectedProfile.trim();
        chats = chats.map((row) => (row.id === targetChatId ? { ...row, agentProfile: stamped } : row));
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

  async function cancelQueuedTurn(turn: PmaQueuedTurn): Promise<void> {
    if (!activeChatId || !turn.managedTurnId) return;
    composeError = null;
    const result = await pmaApi.pma.cancelQueuedTurn(activeChatId, turn.managedTurnId);
    if (result.ok) {
      queuedTurns = queuedTurns.filter((item) => item.managedTurnId !== turn.managedTurnId);
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
    queuedTurns = queuedTurns.filter((item) => item.managedTurnId !== turn.managedTurnId);
    const result = await pmaApi.pma.sendMessage(
      chatId,
      buildManagedThreadMessagePayload(
        turn.prompt,
        turn.model ?? selectedModel,
        true,
        turn.attachments as DocumentFileIntentPayload[],
        turn.reasoning ?? selectedReasoning,
        selectedProfile,
        'interrupt'
      )
    );
    if (result.ok) {
      const optimisticFromBackend = optimisticUserTimelineItemFromSend(
        result.data.raw,
        turn.prompt,
        chatId
      );
      if (optimisticFromBackend) timeline = reconcilePmaTimeline(timeline, [optimisticFromBackend]);
      await refreshActive(chatId, { quiet: true });
    } else {
      composeError = result.error;
    }
  }

  async function autoCompactActiveThread(chatId: string): Promise<void> {
    if (progress?.status === 'running') {
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
    chats = chats.map((chat) => (chat.id === compactResult.data.id ? compactResult.data : chat));
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
    if (spec.destructive && !window.confirm(`Run ${spec.usage}?`)) return true;

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
        artifacts = result.data;
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
        chats = chats.map((chat) => (chat.id === result.data.id ? result.data : chat));
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
        chats = chats.map((chat) => (chat.id === result.data.id ? result.data : chat));
        showCommandNotice('Compaction seed saved.');
        await refreshActive(activeChatId, { quiet: true });
        clearSlashDraft();
      }
      return true;
    }
    if (spec.id === 'archive') {
      const archivedId = activeChatId;
      const result = await pmaApi.pma.archiveThread(archivedId);
      if (!result.ok) composeError = result.error;
      else {
        chats = chats.map((chat) => (chat.id === result.data.id ? result.data : chat));
        showCommandNotice('Thread archived.');
        clearSlashDraft();
        await goto(href('/chats'));
      }
      return true;
    }
    if (spec.id === 'cancel') {
      const turn = queuedTurns.find((item) => String(item.position) === args || item.managedTurnId === args);
      if (!turn) {
        showCommandNotice('Use /cancel with a queued position or turn id.');
        return true;
      }
      await cancelQueuedTurn(turn);
      clearSlashDraft();
      return true;
    }
    if (spec.id === 'clearqueue') {
      const result = await pmaApi.pma.clearQueue(activeChatId);
      if (!result.ok) composeError = result.error;
      else {
        queuedTurns = [];
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
      <div class="filter-row chat-filter-chips-row" aria-label="Chat filters">
        {#each PMA_CHAT_FILTER_ORDER as item}
          {#if item !== 'unread' || filterCounts.unread > 0 || filter === 'unread'}
            <button
              class:active={filter === item}
              class="chip"
              type="button"
              onclick={() => (filter = item)}
            >
              {filterChipLabel(item)}
              <span>{filterCounts[item]}</span>
            </button>
          {/if}
        {/each}
        {#if ticketRunGroupCount > 0 || filter === PMA_CHAT_TICKET_RUNS_FILTER}
          <button
            class:active={filter === PMA_CHAT_TICKET_RUNS_FILTER}
            class="chip"
            type="button"
            onclick={() => (filter = PMA_CHAT_TICKET_RUNS_FILTER)}
          >
            Ticket Runs
            <span>{ticketRunGroupCount}</span>
          </button>
        {/if}
        {#each surfaceFilterChips as surf (surf.slug)}
          {#if surf.count > 0 || filter === pmaChatSurfaceFilterToken(surf.slug)}
            <button
              class:active={filter === pmaChatSurfaceFilterToken(surf.slug)}
              class="chip"
              type="button"
              onclick={() => (filter = pmaChatSurfaceFilterToken(surf.slug))}
            >
              {surf.label}
              <span>{surf.count}</span>
            </button>
          {/if}
        {/each}
      </div>
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
      <button
        class:active={chat.id === activeChatId}
        class:nested
        class={`chat-card status-${chat.status}`}
        type="button"
        onclick={() => selectChat(chat.id)}
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
      </button>
    {/snippet}

    <div class="chat-list-scroll">
      {#if loadingChats}
        <div class="state-panel loading-state">
          <span class="state-icon" aria-hidden="true"></span>
          <strong>Loading chats</strong>
          <p>Fetching managed threads and current run status.</p>
        </div>
      {:else if chatError}
        <div class="state-panel error">
          <strong>Could not load chats</strong>
          <p>{chatError.message}</p>
          <button type="button" onclick={loadInitial}>Retry</button>
        </div>
      {:else}
        {#each filteredEntries as entry (entry.kind === 'group' ? `group:${entry.group.key}` : `chat:${entry.chat.id}`)}
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
                  {#each group.chats as chat (chat.id)}
                    {@render chatRow(chat, true)}
                  {/each}
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
        {/each}
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
  <div class="active-chat">
    <div class="chat-header">
      <div class="chat-header-copy">
        <h1>{activeChat?.title ?? 'Chats'}</h1>
        {#if activeChat}
          <div class="chat-header-scope-line">
            <span class="chat-header-scope-primary">
              {#if activeRepoIngress}
                <a class="chat-header-scope-link" href={href(activeRepoIngress.href)}>
                  <span class="chat-header-scope">{activeRepoIngress.detail}</span>
                  <span class="chat-header-scope-arrow" aria-hidden="true">→</span>
                </a>
              {:else}
                <span class="chat-header-scope">{headerScopeLine}</span>
              {/if}
            </span>
          </div>
          <p class="chat-header-subtitle">
            <span class={`chat-kind-badge ${activeChatKind}`}>{chatAgentDisplayLabel}</span>
            {#if activeMessengerSurface}
              <span class={`chat-surface-badge ${activeMessengerSurface.badgeClass}`}>{activeMessengerSurface.label}</span>
            {/if}
            {#if activeChat.status === 'done' && progress?.elapsedSeconds !== null && progress?.elapsedSeconds !== undefined && statusBar}
              <span class={`status-dot status-${activeChat.status}`} aria-hidden="true"></span>
              <strong class="chat-header-status-strong">{statusLabel(activeChat.status)}</strong>
              {#if statusBar.phase && statusBar.phase.toLowerCase() !== statusLabel(activeChat.status).toLowerCase()}
                <span>{statusBar.phase}</span>
              {/if}
              <span>{statusBar.elapsedLabel}</span>
            {:else if activeChat.status !== 'idle'}
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
      {#if activeChat && showStreamHealthAside}
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

    <div bind:this={messageStack} class="message-stack" aria-live="polite">
      {#if loadingActive && activeChat}
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
          />
        </div>
      {:else}
        <ChatTranscriptCards cards={activeCards} assistantLabel={chatAgentDisplayLabel} />
      {/if}
    </div>

    {#if showStatusBar && statusBar}
      <div class={`pma-status-bar composer-status-bar ${statusBar.state}`} aria-label="Turn status">
        <span class="status-dot" aria-hidden="true"></span>
        <strong>{statusLabel(statusBar.state)}</strong>
        {#if statusBar.phase && statusBar.phase.toLowerCase() !== statusLabel(statusBar.state).toLowerCase()}
          <span>{statusBar.phase}</span>
        {/if}
        {#if progress?.elapsedSeconds !== null && progress?.elapsedSeconds !== undefined}
          <span>{statusBar.elapsedLabel}</span>
        {/if}
        {#if (progress?.queueDepth ?? 0) > 0}
          <span>{statusBar.queueDepthLabel}</span>
        {/if}
        {#if statusBar.tokenUsageLabel}
          <span>{statusBar.tokenUsageLabel}</span>
        {/if}
        {#if statusBar.contextRemainingLabel && statusBar.contextRemainingPercent !== null}
          <span class="context-meter" title="Context remaining">
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
        {sending ? 'Queueing' : progress?.status === 'running' ? 'Queue' : 'Send'}
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
