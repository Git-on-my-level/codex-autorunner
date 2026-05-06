<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount, tick } from 'svelte';
  import SensitiveApprovalCard from '$lib/components/SensitiveApprovalCard.svelte';
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import { openPmaTailEventSource, type StreamSubscription } from '$lib/api/streaming';
  import { mapPmaRunProgress, mapSurfaceArtifact } from '$lib/viewModels/domain';
  import { renderMarkdownToHtml } from '$lib/viewModels/contextspace';
  import type {
    PmaChatMessage,
    PmaChatSummary,
    PmaRunProgress,
    SensitiveApprovalRequest,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    approvalActionUrl,
    buildManagedThreadCreatePayload,
    buildPmaChatScopeOptions,
    buildManagedThreadMessagePayload,
    buildPmaLiveActivity,
    buildPmaCards,
    chooseActiveChatId,
    composeMessageWithAttachments,
    filterSensitiveCarApprovals,
    filterPmaChats,
    formatBytes,
    formatRelativeTime,
    localPmaChatScopeOption,
    mergePmaActivityEvents,
    modelSelectorState,
    pmaChatScopeLabel,
    pmaChatScopeLabelFromChat,
    progressPercent,
    removePendingAttachment,
    statusLabel,
    summarizeFilterCounts,
    type PendingAttachment,
    type PmaCard,
    type PmaChatFilter,
    type PmaChatScopeOption,
    type PmaLiveActivity
  } from '$lib/viewModels/pmaChat';

  let chats = $state<PmaChatSummary[]>([]);
  let messages = $state<PmaChatMessage[]>([]);
  let progress = $state<PmaRunProgress | null>(null);
  let activityEvents = $state<SurfaceArtifact[]>([]);
  let activityRunId = $state<string | null>(null);
  let artifacts = $state<SurfaceArtifact[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let scopeOptions = $state<PmaChatScopeOption[]>(buildPmaChatScopeOptions([], [], []));
  let approvals = $state<SensitiveApprovalRequest[]>([]);
  let pendingAttachments = $state<PendingAttachment[]>([]);
  let localMessageArtifacts = $state<Record<string, SurfaceArtifact[]>>({});
  let linkDialogOpen = $state(false);
  let linkDraft = $state('');
  let activeChatId = $state<string | null>(null);
  let selectedAgent = $state('codex');
  let selectedModel = $state('');
  let selectedScopeId = $state('local');
  let filter = $state<PmaChatFilter>('all');
  let mobilePane = $state<'list' | 'chat'>('list');
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
  let agentError = $state<ApiError | null>(null);
  let modelError = $state<ApiError | null>(null);
  let streamState = $state<'idle' | 'connecting' | 'connected' | 'interrupted'>('idle');
  let streamError = $state<string | null>(null);
  let streamLastEventAt = $state<string | null>(null);
  let streamSubscription: StreamSubscription | null = null;
  let fileInput: HTMLInputElement | null = $state(null);
  let imageInput: HTMLInputElement | null = $state(null);
  let messageStack: HTMLDivElement | null = $state(null);
  let pendingRefreshTimer: number | null = null;
  let lastScrolledChatId: string | null = null;
  let lastScrolledCardCount = 0;

  const activeChat = $derived(chats.find((chat) => chat.id === activeChatId) ?? null);
  const filteredChats = $derived(filterPmaChats(chats, filter, search));
  const filterCounts = $derived(summarizeFilterCounts(chats));
  const visibleMessages = $derived<PmaChatMessage[]>(
    messages.map((message) => ({
      ...message,
      artifacts: [...message.artifacts, ...(localMessageArtifacts[message.id] ?? [])]
    }))
  );
  const activeCards = $derived<PmaCard[]>(buildPmaCards(visibleMessages, progress, activeChat, artifacts, activityEvents));
  const liveActivity = $derived<PmaLiveActivity | null>(buildPmaLiveActivity(progress));
  const modelState = $derived(modelSelectorState(loadingModels, modelError?.message ?? null, models.length));
  const selectedScope = $derived(scopeOptions.find((scope) => scope.id === selectedScopeId) ?? localPmaChatScopeOption());
  const selectedScopeLabel = $derived(pmaChatScopeLabel(selectedScope));
  const activeScopeLabel = $derived(pmaChatScopeLabelFromChat(activeChat));
  const agentStateLabel = $derived(
    agentError ? agentError.message : agents.length === 0 ? 'no agent' : 'agent'
  );

  onMount(() => {
    draft = page.url.searchParams.get('draft') ?? draft;
    void loadInitial();
    const interval = window.setInterval(() => {
      if (activeChatId) void refreshActive(activeChatId, { quiet: true });
    }, 7000);
    return () => window.clearInterval(interval);
  });

  onDestroy(() => {
    if (pendingRefreshTimer) window.clearTimeout(pendingRefreshTimer);
    closeStream();
  });

  $effect(() => {
    const cardCount = activeCards.length + approvals.length;
    const chatChanged = activeChatId !== lastScrolledChatId;
    const cardCountChanged = cardCount !== lastScrolledCardCount;

    if (!activeChat || loadingActive || (!chatChanged && !cardCountChanged)) return;

    const shouldFollowLatest = chatChanged || isMessageStackNearBottom();
    lastScrolledChatId = activeChatId;
    lastScrolledCardCount = cardCount;
    if (shouldFollowLatest) void scrollMessagesToBottom();
  });

  async function loadInitial(): Promise<void> {
    loadingChats = true;
    chatError = null;
    const [chatResult, artifactResult, agentResult, approvalResult, repoResult, worktreeResult, agentWorkspaceResult] = await Promise.all([
      pmaApi.pma.listChats(),
      pmaApi.pma.listFiles(),
      pmaApi.pma.listAgents(),
      pmaApi.settings.listApprovals(),
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.hub.listAgentWorkspaces()
    ]);

    if (chatResult.ok) {
      chats = chatResult.data;
      activeChatId = chooseActiveChatId(chatResult.data, activeChatId, page.url.searchParams.get('chat'));
      if (activeChatId) {
        mobilePane = 'chat';
        void refreshActive(activeChatId);
        connectStream(activeChatId);
      }
    } else {
      chatError = chatResult.error;
    }

    if (artifactResult.ok) artifacts = artifactResult.data;
    if (approvalResult.ok) approvals = filterSensitiveCarApprovals(approvalResult.data);
    scopeOptions = buildPmaChatScopeOptions(
      repoResult.ok ? repoResult.data : [],
      worktreeResult.ok ? worktreeResult.data : [],
      agentWorkspaceResult.ok ? agentWorkspaceResult.data : []
    );
    if (!scopeOptions.some((scope) => scope.id === selectedScopeId)) selectedScopeId = 'local';
    if (agentResult.ok) {
      agents = agentResult.data;
      selectedAgent =
        stringField(agentResult.data[0], 'id') ?? stringField(agentResult.data[0], 'agent') ?? selectedAgent;
      void loadModels(selectedAgent);
    } else {
      agentError = agentResult.error;
    }
    loadingChats = false;
  }

  async function refreshApprovals(): Promise<void> {
    const result = await pmaApi.settings.listApprovals();
    if (result.ok) approvals = filterSensitiveCarApprovals(result.data);
  }

  async function loadModels(agentId: string): Promise<void> {
    loadingModels = true;
    modelError = null;
    const result = await pmaApi.pma.listAgentModels(agentId);
    if (!result.ok) {
      models = [];
      selectedModel = '';
      modelError = result.error;
      loadingModels = false;
      return;
    }
    models = result.data;
    selectedModel = stringField(result.data[0], 'id') ?? stringField(result.data[0], 'model') ?? '';
    loadingModels = false;
  }

  async function selectChat(chatId: string): Promise<void> {
    activeChatId = chatId;
    resetActivityEvents();
    mobilePane = 'chat';
    await refreshActive(chatId);
    connectStream(chatId);
  }

  async function refreshActive(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    if (!options.quiet) {
      loadingActive = true;
      activeError = null;
    }
    const [messageResult, tailResult, statusResult] = await Promise.all([
      pmaApi.pma.getMessages(chatId),
      pmaApi.pma.getTail(chatId),
      pmaApi.pma.getStatus(chatId)
    ]);

    if (activeChatId !== chatId) return;
    if (messageResult.ok) messages = messageResult.data;
    else if (!options.quiet) activeError = messageResult.error;

    if (tailResult.ok) updateProgress(tailResult.data);
    else if (statusResult.ok) updateProgress(statusResult.data);
    else if (!options.quiet) activeError = tailResult.error;

    void refreshApprovals();
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
        streamLastEventAt = new Date().toISOString();
        if (event.kind === 'tail') {
          activityEvents = mergePmaActivityEvents(activityEvents, [mapSurfaceArtifact(event.payload)]);
        }
        if (event.kind === 'progress' || event.kind === 'state') {
          updateProgress(mapPmaRunProgress(event.payload));
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
        streamState = 'interrupted';
        streamError = 'Live PMA updates were interrupted. Polling continues in the background.';
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

  function updateProgress(nextProgress: PmaRunProgress): void {
    progress = nextProgress;
    const nextRunId = nextProgress.id || activeChatId;
    if (activityRunId !== nextRunId) {
      activityRunId = nextRunId;
      activityEvents = [];
    }
    activityEvents = mergePmaActivityEvents(activityEvents, nextProgress.events);
  }

  function resetActivityEvents(): void {
    activityEvents = [];
    activityRunId = null;
  }

  function retryStream(): void {
    if (!activeChatId) return;
    connectStream(activeChatId);
    void refreshActive(activeChatId, { quiet: true });
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
    const scopedAgent = selectedScope?.kind === 'agent_workspace' && selectedScope.agentId ? selectedScope.agentId : selectedAgent;
    const result = await pmaApi.pma.createChat(buildManagedThreadCreatePayload(scopedAgent, selectedScope));
    if (result.ok) {
      chats = [result.data, ...chats.filter((chat) => chat.id !== result.data.id)];
      await selectChat(result.data.id);
    } else {
      composeError = result.error;
    }
    creating = false;
  }

  async function sendMessage(): Promise<void> {
    if ((!draft.trim() && pendingAttachments.length === 0) || !activeChatId) return;
    sending = true;
    composeError = null;
    const uploaded = await ensureAttachmentsUploaded();
    if (!uploaded) {
      sending = false;
      return;
    }
    const attachmentsForMessage = pendingAttachments;
    const message = composeMessageWithAttachments(draft, attachmentsForMessage);
    const result = await pmaApi.pma.sendMessage(
      activeChatId,
      buildManagedThreadMessagePayload(message, selectedModel, activeChat?.status === 'running', attachmentsForMessage)
    );
    if (result.ok) {
      draft = '';
      pendingAttachments = [];
      messages = [...messages, result.data];
      await refreshActive(activeChatId, { quiet: true });
    } else {
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

  async function decideApproval(
    approval: SensitiveApprovalRequest,
    decision: 'approve' | 'decline'
  ): Promise<void> {
    composeError = null;
    const url = approvalActionUrl(approval, decision);
    if (!url) {
      composeError = {
        kind: 'parse',
        status: null,
        code: 'approval_route_missing',
        message: 'This approval is visible, but the backend did not expose an approve/decline route.'
      };
      return;
    }
    const body =
      url === approval.raw.decision_url || url === approval.raw.route
        ? { decision, approval_id: approval.id }
        : undefined;
    const result = await pmaApi.requestJson<JsonRecord>(url, {
      method: 'POST',
      body
    });
    if (result.ok) {
      approvals = approvals.filter((item) => item.id !== approval.id);
      void refreshApprovals();
    } else {
      composeError = result.error;
    }
  }

  function agentLabel(agent: JsonRecord): string {
    return stringField(agent, 'label') ?? stringField(agent, 'name') ?? stringField(agent, 'id') ?? 'Agent';
  }

  function modelLabel(model: JsonRecord): string {
    return stringField(model, 'label') ?? stringField(model, 'name') ?? stringField(model, 'id') ?? stringField(model, 'model') ?? 'Model';
  }

  function stringField(record: JsonRecord | undefined, key: string): string | null {
    const value = record?.[key];
    return typeof value === 'string' && value.trim() ? value : null;
  }

</script>

<section class="pma-layout" aria-label="PMA chat workspace">
  <div class="pma-mobile-switch" role="tablist" aria-label="PMA chat panels">
    <button
      class:active={mobilePane === 'list'}
      type="button"
      role="tab"
      aria-selected={mobilePane === 'list'}
      onclick={() => (mobilePane = 'list')}
    >
      Chats
    </button>
    <button
      class:active={mobilePane === 'chat'}
      type="button"
      role="tab"
      aria-selected={mobilePane === 'chat'}
      onclick={() => (mobilePane = 'chat')}
      disabled={!activeChat}
    >
      Active
    </button>
  </div>

  <aside class:hidden-mobile-pane={mobilePane !== 'list'} class="chat-list" aria-label="PMA chats">
    <div class="chat-list-header">
      <div class="section-heading">
        <h2>Conversations</h2>
      </div>
      <div class="new-chat-controls">
        <label class="scope-field">
          <span>Scope</span>
          <select aria-label="PMA chat scope" bind:value={selectedScopeId} disabled={creating}>
            {#each scopeOptions as scope (scope.id)}
              <option value={scope.id}>{scope.detail}: {scope.label}</option>
            {/each}
          </select>
        </label>
        <button class="new-chat-button" type="button" onclick={createChat} disabled={creating}>
          {creating ? 'Creating' : 'New chat'}
        </button>
      </div>
    </div>
    <p class="selected-scope-note">{selectedScopeLabel}</p>

    <label class="search-field">
      <span class="sr-only">Search PMA chats</span>
      <input bind:value={search} type="search" placeholder="Search chats, repos, tickets" />
    </label>

    <div class="filter-row" aria-label="Chat status filters">
      {#each ['all', 'active', 'waiting', 'done'] as item}
        <button
          class:active={filter === item}
          class="chip"
          type="button"
          onclick={() => (filter = item as PmaChatFilter)}
        >
          {item}
          <span>{filterCounts[item as PmaChatFilter]}</span>
        </button>
      {/each}
    </div>

    {#if loadingChats}
      <div class="state-panel loading-state">
        <span class="state-icon" aria-hidden="true"></span>
        <strong>Loading PMA chats</strong>
        <p>Fetching managed threads and current run status.</p>
      </div>
    {:else if chatError}
      <div class="state-panel error">
        <strong>Could not load PMA chats</strong>
        <p>{chatError.message}</p>
        <button type="button" onclick={loadInitial}>Retry</button>
      </div>
    {:else if filteredChats.length === 0}
      <div class="state-panel empty-state">
        <strong>No PMA chats match this view</strong>
        <p>Clear the search or choose a broader status filter.</p>
      </div>
    {:else}
      <div class="chat-list-scroll">
        {#each filteredChats as chat (chat.id)}
          <button
            class:active={chat.id === activeChatId}
            class={`chat-card status-${chat.status}`}
            type="button"
            onclick={() => selectChat(chat.id)}
          >
            <span class="chat-card-main">
              <span class="chat-title-row">
                <strong>{chat.title}</strong>
                <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
              </span>
              <span class="chat-meta-row">
                <span class="chat-id-tag">#{chat.id.slice(0, 6)}</span>
                <span class="chat-meta-dot" aria-hidden="true">·</span>
                <span class="chat-scope">{pmaChatScopeLabelFromChat(chat)}</span>
                {#if chat.worktreeId}
                  <span class="chat-meta-dot" aria-hidden="true">·</span>
                  <span>{chat.worktreeId}</span>
                {/if}
                {#if chat.ticketId}
                  <span class="chat-meta-dot" aria-hidden="true">·</span>
                  <code>{chat.ticketId}</code>
                {/if}
                {#if chat.model}
                  <span class="chat-meta-dot" aria-hidden="true">·</span>
                  <span class="chat-model">{chat.model}</span>
                {/if}
              </span>
              <span class="chat-card-footer">
                <span class={`progress-track status-${chat.status}`} aria-label={`${progressPercent(chat)} percent complete`}>
                  <span style={`width: ${progressPercent(chat)}%`}></span>
                </span>
                <span class="updated-at">{formatRelativeTime(chat.updatedAt)}</span>
              </span>
            </span>
          </button>
        {/each}
      </div>
    {/if}
  </aside>

  <div class:hidden-mobile-pane={mobilePane !== 'chat'} class="active-chat">
    <div class="chat-header">
      <div class="chat-header-copy">
        <h1>{activeChat?.title ?? 'PMA'}</h1>
        {#if activeChat}
          <p class="chat-header-subtitle">
            <span class={`status-dot status-${activeChat.status}`} aria-hidden="true"></span>
            {statusLabel(activeChat.status)}
            <span class="chat-meta-dot" aria-hidden="true">·</span>
            <span class="chat-header-scope">{activeScopeLabel}</span>
            {#if activeChat.ticketId}
              <span class="chat-meta-dot" aria-hidden="true">·</span>
              <code>{activeChat.ticketId}</code>
            {/if}
            <span class="chat-meta-dot" aria-hidden="true">·</span>
            <span class="chat-header-id">#{activeChat.id.slice(0, 6)}</span>
          </p>
        {/if}
      </div>
      <div class="selector-row">
        <label class="selector-field">
          <span>{agentStateLabel}</span>
          <select
            aria-label="PMA agent"
            bind:value={selectedAgent}
            disabled={agents.length === 0}
            onchange={() => loadModels(selectedAgent)}
          >
          {#if agents.length === 0}
            <option value={selectedAgent}>{agentError ? 'Unavailable' : 'Configured agent'}</option>
          {:else}
            {#each agents as agent}
              <option value={stringField(agent, 'id') ?? stringField(agent, 'agent') ?? agentLabel(agent)}>
                {agentLabel(agent)}
              </option>
            {/each}
          {/if}
          </select>
        </label>
        <label class={`selector-field ${modelState.state}`}>
          <span>{modelState.label}</span>
          <select aria-label="Model" bind:value={selectedModel} disabled={modelState.disabled}>
          {#if models.length === 0}
            <option value="">Configured model</option>
          {:else}
            {#each models as model}
              <option value={stringField(model, 'id') ?? stringField(model, 'model') ?? modelLabel(model)}>
                {modelLabel(model)}
              </option>
            {/each}
          {/if}
          </select>
        </label>
      </div>
    </div>

    {#if activeChat}
      <div class={`stream-health ${streamState}`} role="status">
        <span class="status-dot" aria-hidden="true"></span>
        <span>
          {#if streamState === 'connected'}
            Live updates connected{streamLastEventAt ? ` · ${formatRelativeTime(streamLastEventAt)}` : ''}
          {:else if streamState === 'connecting'}
            Connecting live updates
          {:else if streamState === 'interrupted'}
            {streamError}
          {:else}
            Live updates idle
          {/if}
        </span>
        {#if streamState === 'interrupted'}
          <button type="button" onclick={retryStream}>Reconnect</button>
        {/if}
      </div>
    {/if}

    <div bind:this={messageStack} class="message-stack" aria-live="polite">
      {#if loadingActive}
        <div class="state-panel loading-state">
          <span class="state-icon" aria-hidden="true"></span>
          <strong>Loading active chat</strong>
          <p>Collecting messages, tail events, and approval prompts.</p>
        </div>
      {:else if activeError}
        <div class="state-panel error">
          <strong>Could not load this chat</strong>
          <p>{activeError.message}</p>
          <button type="button" onclick={() => activeChatId && refreshActive(activeChatId)}>Retry</button>
        </div>
      {:else if !activeChat}
        <div class="state-panel empty-state">
          <strong>No PMA chat is selected</strong>
          <p>Pick a conversation or create a new chat to start work.</p>
          <button type="button" onclick={() => (mobilePane = 'list')}>Browse chats</button>
        </div>
      {:else if activeCards.length === 0 && approvals.length === 0}
        <div class="state-panel empty-state">
          <strong>This chat is ready</strong>
          <p>Send a message or attach files so PMA can start from current context.</p>
        </div>
      {:else}
        {#if liveActivity && (liveActivity.state === 'running' || liveActivity.state === 'waiting' || liveActivity.state === 'blocked')}
          <section class={`live-activity ${liveActivity.state}`} aria-label="PMA live activity">
            <div class="live-activity-header">
              <span class="live-pulse" aria-hidden="true"></span>
              <div>
                <strong>{liveActivity.title}</strong>
                <p>{liveActivity.summary}</p>
              </div>
              {#if liveActivity.elapsedLabel}
                <span class="live-activity-time">{liveActivity.elapsedLabel}</span>
              {/if}
            </div>
            {#if liveActivity.steps.length > 0}
              <ol class="live-step-list" aria-label="Recent PMA steps">
                {#each liveActivity.steps as step (step.id)}
                  <li>
                    <span>{step.title}</span>
                    {#if step.summary}<small>{step.summary}</small>{/if}
                  </li>
                {/each}
              </ol>
            {/if}
          </section>
        {/if}
        {#each approvals as approval (approval.id)}
          <SensitiveApprovalCard {approval} onDecision={decideApproval} />
        {/each}
        {#each activeCards as card (card.id)}
          {#if card.kind === 'message'}
            <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}`}>
              <span>{card.message.role === 'user' ? 'You' : 'PMA'}</span>
              <div class="message-markdown markdown-body">
                {@html renderMarkdownToHtml(card.message.text)}
              </div>
            </article>
          {:else if card.kind === 'intermediate'}
            <article class="intermediate-card" aria-label="PMA intermediate output">
              <span class="artifact-type">PMA update</span>
              <strong>{card.title}</strong>
              <div class="message-markdown markdown-body">
                {@html renderMarkdownToHtml(card.text)}
              </div>
            </article>
          {:else if card.kind === 'tool_group'}
            <details class="tool-call-bar">
              <summary>
                <span>Tool calls</span>
                <strong>{card.tools.length} {card.tools.length === 1 ? 'tool call' : 'tool calls'}</strong>
              </summary>
              <ol>
                {#each card.tools as tool (tool.id)}
                  <li class={tool.state}>
                    <span>{tool.state}</span>
                    <strong>{tool.title}</strong>
                    {#if tool.summary && tool.summary !== tool.title}
                      <small>{tool.summary}</small>
                    {/if}
                  </li>
                {/each}
              </ol>
            </details>
          {:else if card.kind === 'ticket'}
            <article class="artifact-card ticket-card">
              <span class="artifact-type">Ticket</span>
              <strong>{card.title}</strong>
              <p>{card.summary ?? 'PMA created or is managing this ticket.'}</p>
            </article>
          {:else}
            <SurfaceArtifactCard artifact={card.artifact} />
          {/if}
        {/each}
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
        aria-label="Message PMA"
        bind:value={draft}
        disabled={!activeChat || sending}
        placeholder={activeChat ? 'Message PMA...' : 'Create or select a PMA chat'}
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
    <p class="permission-note">PMA has full permission for normal coding work. Sensitive CAR operations require approval.</p>
    {#if composeError}
      <p class="compose-error">{composeError.message}</p>
    {/if}
  </div>
</section>
