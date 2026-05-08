<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount, tick } from 'svelte';
  import MasterDetail from '$lib/components/MasterDetail.svelte';
  import ChatTranscriptCards from '$lib/components/ChatTranscriptCards.svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import ModelReasoningPicker from '$lib/components/ModelReasoningPicker.svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { openPmaTailEventSource, type StreamSubscription } from '$lib/api/streaming';
  import { repoRoute, worktreeRoute } from '$lib/viewModels/routes';
  import { mapPmaRunProgress, mapPmaTimelineItem } from '$lib/viewModels/domain';
  import type {
    PmaChatSummary,
    PmaRunProgress,
    PmaTimelineItem,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    buildManagedThreadCreatePayload,
    buildPmaChatScopeOptions,
    buildManagedThreadMessagePayload,
    buildPmaTranscriptCards,
    buildPmaStatusBar,
    chooseActiveChatId,
    composeMessageWithAttachments,
    filterPmaChats,
    formatBytes,
    formatRelativeTime,
    localPmaChatScopeOption,
    PMA_CHAT_FILTER_ORDER,
    pmaChatKind,
    pmaChatKindLabel,
    pmaChatHeaderScopeLine,
    pmaChatScopeTagView,
    progressPercent,
    reconcilePmaTimeline,
    removePendingAttachment,
    sortChatsWaitingFirst,
    statusLabel,
    summarizeFilterCounts,
    type PendingAttachment,
    type PmaCard,
    type PmaChatFilter,
    type PmaChatScopeOption
  } from '$lib/viewModels/pmaChat';
  import {
    isChatUnread,
    loadLastSeenMap,
    markChatRead,
    saveLastSeenMap,
    type ChatLastSeenMap
  } from '$lib/viewModels/unread';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import {
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

  let chats = $state<PmaChatSummary[]>([]);
  let timeline = $state<PmaTimelineItem[]>([]);
  let progress = $state<PmaRunProgress | null>(null);
  let artifacts = $state<SurfaceArtifact[]>([]);
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
  const filteredChats = $derived(sortChatsWaitingFirst(filterPmaChats(chats, filter, search, lastSeenMap)));
  const filterCounts = $derived(summarizeFilterCounts(chats, lastSeenMap));
  const activeCards = $derived<PmaCard[]>(buildPmaTranscriptCards(timeline, activeChat, artifacts, progress));
  const statusBar = $derived(buildPmaStatusBar(progress, activeChat));
  const selectedScope = $derived(scopeOptions.find((scope) => scope.id === selectedScopeId) ?? localPmaChatScopeOption());
  const selectedAgentRecord = $derived(agentRecordForId(agents, selectedAgent));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const selectedModelRecord = $derived(modelRecordForValue(models, selectedModel));
  const reasoningOptions = $derived(pickerReasoningOptions(models, selectedModel));
  const showAgentSelector = $derived(Boolean(activeChat && agents.length > 0));
  const showModelSelector = $derived(Boolean(activeChat && selectedAgentCanListModels && (loadingModels || models.length > 0)));
  const showEffortSelector = $derived(Boolean(showModelSelector && reasoningOptions.length > 0));
  const canStartCodingAgentChat = $derived(selectedScope.kind !== 'local');
  const activeChatKind = $derived(pmaChatKind(activeChat));
  const activeChatKindLabel = $derived(pmaChatKindLabel(activeChatKind));
  const activeRepoIngress = $derived(repoIngressForChat(activeChat));
  const createChatLabel = $derived(
    creating ? 'Creating...' : newChatKind === 'agent' && canStartCodingAgentChat ? '+ Coding agent' : '+ PMA chat'
  );
  const headerScopeLine = $derived(pmaChatHeaderScopeLine(activeChat, repoLabelForRepoId));
  /** Omit connected “Live · …” — redundant with the PMA turn pill on the scope row. */
  const showStreamHealthAside = $derived(
    streamState === 'connecting' || streamState === 'interrupted'
  );
  const showStatusBar = $derived(
    Boolean(
      statusBar &&
        statusBar.state !== 'idle' &&
        statusBar.state !== 'done' &&
        ((progress?.elapsedSeconds !== null && progress?.elapsedSeconds !== undefined) ||
          (progress?.queueDepth ?? 0) > 0 ||
          ['running', 'waiting', 'blocked', 'failed'].includes(statusBar.state))
    )
  );
  const chatHasActivity = $derived(activeCards.length > 0 || showStatusBar);
  const showStartPicker = $derived(Boolean(activeChat) && !loadingActive && !activeError && !chatHasActivity);
  const selectedAgentLabel = $derived(selectedAgentRecord ? agentLabel(selectedAgentRecord) : selectedAgent || 'Agent');
  const selectedModelLabel = $derived(selectedModelRecord ? modelLabel(selectedModelRecord) : selectedModel || '');
  const selectedEffortLabel = $derived(selectedReasoning || 'default');

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

  function chatUsesRepoGlyph(chat: PmaChatSummary): boolean {
    return Boolean(chat.repoId || chat.worktreeId);
  }

  function filterChipLabel(key: PmaChatFilter): string {
    return key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1);
  }

  function composerRecipientLabel(chat: PmaChatSummary | null): string {
    if (!chat) return 'PMA';
    if (chat.repoId) {
      const repoLabel = repoLabelForRepoId(chat.repoId);
      return repoLabel ?? chat.repoId;
    }
    if (chat.worktreeId) {
      const opt = worktreeScopeOption(chat.worktreeId);
      return opt?.label ?? chat.worktreeId;
    }
    return 'PMA';
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
      agents = agentResult.data;
      if (!activeChat?.agentId) {
        selectedAgent =
          agentResult.data[0] ? agentId(agentResult.data[0]) : selectedAgent;
      }
      void loadModels(selectedAgent, activeChat?.model ?? selectedModel);
    }
    applyNewChatQueryParam();
    loadingChats = false;
  }

  function applyNewChatQueryParam(): void {
    // Scoped repo/worktree launches live on the repo pages; Chats only exposes hub PMA creation by default.
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
    const [messageResult, tailResult, statusResult] = await Promise.all([
      pmaApi.pma.getTimeline(chatId),
      pmaApi.pma.getTail(chatId),
      pmaApi.pma.getStatus(chatId)
    ]);

    if (activeChatId !== chatId) return;
    if (messageResult.ok) timeline = reconcilePmaTimeline(timeline, messageResult.data);
    else if (!options.quiet) activeError = messageResult.error;

    if (tailResult.ok) updateProgress(tailResult.data);
    else if (statusResult.ok) updateProgress(statusResult.data);
    else if (!options.quiet) activeError = tailResult.error;

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
        streamError = 'Live PMA updates were interrupted. Polling continues in the background.';
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
    selectedReasoning = stringField(chat.raw, 'reasoning') ?? '';
    void loadModels(chat.agentId, chat.model ?? selectedModel);
  }

  function handleAgentChange(): void {
    void loadModels(selectedAgent);
  }

  function newChatDisplayName(): string {
    return newChatKind === 'agent' && canStartCodingAgentChat
      ? 'New coding agent chat'
      : 'New PMA chat';
  }

  async function ensureChatForSelectedAgent(): Promise<string | null> {
    if (!activeChat?.agentId || activeChat.agentId === selectedAgent) return activeChatId;
    const result = await pmaApi.pma.createChat(buildManagedThreadCreatePayload(selectedAgent, selectedScope, newChatDisplayName(), selectedModel));
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
    const result = await pmaApi.pma.createChat(buildManagedThreadCreatePayload(selectedAgent, selectedScope, newChatDisplayName(), selectedModel));
    if (result.ok) {
      chats = [result.data, ...chats.filter((chat) => chat.id !== result.data.id)];
      await selectChat(result.data.id);
      selectedScopeId = 'local';
      newChatKind = 'pma';
    } else {
      composeError = result.error;
    }
    creating = false;
  }

  async function sendMessage(): Promise<void> {
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
    sending = true;
    composeError = null;

    const removeOptimistic = () => {
      timeline = timeline.filter((item) => !item.id.startsWith('optimistic:'));
    };
    const restoreDraft = () => {
      removeOptimistic();
      draft = draftSnapshot;
      pendingAttachments = attachmentsSnapshot;
    };

    const uploaded = await ensureAttachmentsUploaded();
    if (!uploaded) {
      restoreDraft();
      sending = false;
      return;
    }
    const attachmentsForMessage = pendingAttachments;
    const message = composeMessageWithAttachments(draftSnapshot, attachmentsForMessage);
    const targetChatId = await ensureChatForSelectedAgent();
    if (!targetChatId) {
      restoreDraft();
      sending = false;
      return;
    }
    const targetIsRunning = targetChatId === activeChatId && activeChat?.status === 'running';
    const result = await pmaApi.pma.sendMessage(
      targetChatId,
      buildManagedThreadMessagePayload(message, selectedModel, targetIsRunning, attachmentsForMessage, selectedReasoning)
    );
    if (result.ok) {
      await refreshActive(targetChatId, { quiet: true });
      removeOptimistic();
      pendingAttachments = [];
    } else {
      restoreDraft();
      composeError = result.error;
    }
    sending = false;
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
    cancelLinkDialog();
  }

  function removeAttachment(attachmentId: string): void {
    pendingAttachments = removePendingAttachment(pendingAttachments, attachmentId);
  }

  function handlePaste(event: ClipboardEvent): void {
    const files = Array.from(event.clipboardData?.files ?? []).filter((file) => file.type.startsWith('image/'));
    if (files.length) addFiles(files, 'image');
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      void sendMessage();
    }
  }

  async function ensureAttachmentsUploaded(): Promise<boolean> {
    const uploaded: PendingAttachment[] = [];
    for (const attachment of pendingAttachments) {
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
        pendingAttachments = pendingAttachments.map((item) =>
          item.id === attachment.id ? { ...item, uploadState: 'error' } : item
        );
        return false;
      }
      const uploadedName = result.data[0];
      uploaded.push({
        ...attachment,
        uploadedName,
        url: `/hub/pma/files/inbox/${encodeURIComponent(uploadedName)}`,
        uploadState: 'uploaded'
      });
    }
    pendingAttachments = uploaded;
    return true;
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

    <div class="filter-row" aria-label="Chat status filters">
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
    </div>

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
        {#each filteredChats as chat (chat.id)}
          {@const scopeTags = pmaChatScopeTagView(chat, {
            repoLabel: repoLabelForRepoId,
            worktreeLabel: (wid) => worktreeScopeOption(wid)?.label ?? null
          })}
          {@const listAgentLabel = agentDisplayForChat(agents, chat)}
          <button
            class:active={chat.id === activeChatId}
            class={`chat-card status-${chat.status}`}
            type="button"
            onclick={() => selectChat(chat.id)}
          >
            {#if chatUsesRepoGlyph(chat)}
              {@const glabel = chatRepoGlyphLabel(chat)}
              <span
                class="chat-row-glyph repo-mini-glyph"
                style={`--glyph-accent: ${repoAccent(glabel)}`}
                aria-hidden="true"
              >{repoInitials(glabel)}</span>
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
                    <strong>{chat.title}</strong>
                    <span class={`chat-kind-badge ${pmaChatKind(chat)}`}>{pmaChatKindLabel(pmaChatKind(chat))}</span>
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
                <span class="chat-scope-tags">
                  <span class={`chat-scope-kind-tag ${scopeTags.kindKey}`}>{scopeTags.kindLabel}</span>
                  <span class="chat-scope-detail-tag" title={scopeTags.detail}>{scopeTags.detail}</span>
                </span>
                {#if chat.ticketId}
                  <span class="chat-meta-dot" aria-hidden="true">·</span>
                  <code>{chat.ticketId}</code>
                {/if}
                {#if listAgentLabel || chat.model}
                  <span class="chat-meta-dot" aria-hidden="true">·</span>
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
        {/each}
        {#if filteredChats.length === 0}
          <div class="state-panel empty-state compact-empty chat-filter-empty">
            <strong>No chats match this filter</strong>
            <p>Clear the search or choose another status.</p>
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
            {#if showStatusBar && statusBar}
              <div class={`pma-status-bar ${statusBar.state}`} aria-label="PMA turn status">
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
              </div>
            {/if}
          </div>
          <p class="chat-header-subtitle">
            <span class={`chat-kind-badge ${activeChatKind}`}>{activeChatKindLabel}</span>
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
          {#if showAgentSelector}
            <label class="start-picker-row">
              <span>agent</span>
              <select
                aria-label="Agent"
                bind:value={selectedAgent}
                onchange={handleAgentChange}
              >
                {#each agents as agent}
                  <option value={agentId(agent)}>
                    {agentLabel(agent)}
                  </option>
                {/each}
              </select>
            </label>
          {/if}
          <ModelReasoningPicker
            bind:modelValue={selectedModel}
            bind:reasoningValue={selectedReasoning}
            {models}
            loading={loadingModels}
            showModel={showModelSelector}
            showReasoning={showEffortSelector}
          />
        </div>
      {:else}
        <ChatTranscriptCards cards={activeCards} />
      {/if}
    </div>

    <form
      class="composer"
      onpaste={handlePaste}
      onsubmit={(event) => {
        event.preventDefault();
        void sendMessage();
      }}
    >
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
      </div>
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
      <textarea
        aria-label={activeChat ? `Message ${composerRecipientLabel(activeChat)}` : 'Message chat'}
        bind:value={draft}
        disabled={!activeChat || sending}
        placeholder={activeChat ? `Message ${composerRecipientLabel(activeChat)}...` : 'Create or select a chat'}
        onkeydown={handleComposerKeydown}
      ></textarea>
      <button class="send-button" type="submit" disabled={!activeChat || (!draft.trim() && pendingAttachments.length === 0) || sending}>
        {sending ? 'Sending' : 'Send'}
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
  </div>
  {/snippet}
</MasterDetail>
