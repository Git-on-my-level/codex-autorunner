<script lang="ts">
  import { onMount } from 'svelte';
  import SensitiveApprovalCard from '$lib/components/SensitiveApprovalCard.svelte';
  import SurfaceArtifactCard from '$lib/components/SurfaceArtifactCard.svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import type {
    PmaChatMessage,
    PmaChatSummary,
    PmaRunProgress,
    SensitiveApprovalRequest,
    SurfaceArtifact
  } from '$lib/viewModels/domain';
  import {
    approvalActionUrl,
    buildPmaCards,
    chooseActiveChatId,
    composeMessageWithAttachments,
    filterSensitiveCarApprovals,
    filterPmaChats,
    formatBytes,
    formatRelativeTime,
    modelSelectorState,
    progressPercent,
    removePendingAttachment,
    statusLabel,
    summarizeFilterCounts,
    type PendingAttachment,
    type PmaCard,
    type PmaChatFilter
  } from '$lib/viewModels/pmaChat';

  let chats = $state<PmaChatSummary[]>([]);
  let messages = $state<PmaChatMessage[]>([]);
  let progress = $state<PmaRunProgress | null>(null);
  let artifacts = $state<SurfaceArtifact[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
  let approvals = $state<SensitiveApprovalRequest[]>([]);
  let pendingAttachments = $state<PendingAttachment[]>([]);
  let localMessageArtifacts = $state<Record<string, SurfaceArtifact[]>>({});
  let activeChatId = $state<string | null>(null);
  let selectedAgent = $state('codex');
  let selectedModel = $state('');
  let filter = $state<PmaChatFilter>('all');
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
  let fileInput: HTMLInputElement | null = $state(null);
  let imageInput: HTMLInputElement | null = $state(null);

  const activeChat = $derived(chats.find((chat) => chat.id === activeChatId) ?? null);
  const filteredChats = $derived(filterPmaChats(chats, filter, search));
  const filterCounts = $derived(summarizeFilterCounts(chats));
  const visibleMessages = $derived<PmaChatMessage[]>(
    messages.map((message) => ({
      ...message,
      artifacts: [...message.artifacts, ...(localMessageArtifacts[message.id] ?? [])]
    }))
  );
  const activeCards = $derived<PmaCard[]>(buildPmaCards(visibleMessages, progress, activeChat, artifacts));
  const modelState = $derived(modelSelectorState(loadingModels, modelError?.message ?? null, models.length));
  const agentStateLabel = $derived(
    agentError ? agentError.message : agents.length === 0 ? 'No PMA agents exposed' : 'PMA agent'
  );

  onMount(() => {
    void loadInitial();
    const interval = window.setInterval(() => {
      if (activeChatId) void refreshActive(activeChatId, { quiet: true });
    }, 7000);
    return () => window.clearInterval(interval);
  });

  async function loadInitial(): Promise<void> {
    loadingChats = true;
    chatError = null;
    const [chatResult, artifactResult, agentResult, approvalResult] = await Promise.all([
      pmaApi.pma.listChats(),
      pmaApi.pma.listFiles(),
      pmaApi.pma.listAgents(),
      pmaApi.settings.listApprovals()
    ]);

    if (chatResult.ok) {
      chats = chatResult.data;
      activeChatId = chooseActiveChatId(chatResult.data, activeChatId);
      if (activeChatId) void refreshActive(activeChatId);
    } else {
      chatError = chatResult.error;
    }

    if (artifactResult.ok) artifacts = artifactResult.data;
    if (approvalResult.ok) approvals = filterSensitiveCarApprovals(approvalResult.data);
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
    await refreshActive(chatId);
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

    if (tailResult.ok) progress = tailResult.data;
    else if (statusResult.ok) progress = statusResult.data;
    else if (!options.quiet) activeError = tailResult.error;

    void refreshApprovals();
    loadingActive = false;
  }

  async function createChat(): Promise<void> {
    creating = true;
    composeError = null;
    const result = await pmaApi.pma.createChat({
      agent: selectedAgent || undefined,
      model: selectedModel || undefined,
      name: 'New PMA chat'
    });
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
    const result = await pmaApi.pma.sendMessage(activeChatId, {
      message,
      model: selectedModel || undefined,
      busy_policy: activeChat?.status === 'running' ? 'queue' : undefined
    });
    if (result.ok) {
      draft = '';
      pendingAttachments = [];
      const messageArtifacts = attachmentsForMessage.map(surfaceArtifactFromAttachment);
      localMessageArtifacts = {
        ...localMessageArtifacts,
        [result.data.id]: messageArtifacts
      };
      messages = [...messages, { ...result.data, artifacts: [...result.data.artifacts, ...messageArtifacts] }];
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

  function addLink(): void {
    const href = window.prompt('Attach link');
    if (!href?.trim()) return;
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
  }

  function removeAttachment(attachmentId: string): void {
    pendingAttachments = removePendingAttachment(pendingAttachments, attachmentId);
  }

  function handlePaste(event: ClipboardEvent): void {
    const files = Array.from(event.clipboardData?.files ?? []).filter((file) => file.type.startsWith('image/'));
    if (files.length) addFiles(files, 'image');
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

  function surfaceArtifactFromAttachment(attachment: PendingAttachment): SurfaceArtifact {
    return {
      id: attachment.uploadedName ?? attachment.id,
      kind: attachment.kind === 'image' ? 'image' : attachment.kind === 'link' ? 'link' : 'file',
      title: attachment.title,
      summary: attachment.sizeLabel,
      url: attachment.url,
      createdAt: new Date().toISOString(),
      raw: attachment
    };
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
  <aside class="chat-list" aria-label="PMA chats">
    <div class="chat-list-header">
      <div class="section-heading">
        <p class="eyebrow">PMA chats</p>
        <h1>Conversations</h1>
      </div>
      <button class="new-chat-button" type="button" onclick={createChat} disabled={creating}>
        {creating ? 'Creating' : 'New chat'}
      </button>
    </div>

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
      <div class="state-panel">Loading PMA chats...</div>
    {:else if chatError}
      <div class="state-panel error">Could not load PMA chats. {chatError.message}</div>
    {:else if filteredChats.length === 0}
      <div class="state-panel">No PMA chats match this view.</div>
    {:else}
      <div class="chat-list-scroll">
        {#each filteredChats as chat (chat.id)}
          <button
            class:active={chat.id === activeChatId}
            class="chat-card"
            type="button"
            onclick={() => selectChat(chat.id)}
          >
            <span class="chat-card-main">
              <span class="chat-title-row">
                <strong>{chat.title}</strong>
                <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
              </span>
              <span class="chat-meta-row">
                {#if chat.repoId}<span>{chat.repoId}</span>{/if}
                {#if chat.worktreeId}<span>{chat.worktreeId}</span>{/if}
                {#if chat.ticketId}<span>{chat.ticketId}</span>{/if}
              </span>
              <span class="progress-track" aria-label={`${progressPercent(chat)} percent complete`}>
                <span style={`width: ${progressPercent(chat)}%`}></span>
              </span>
              <span class="updated-at">{formatRelativeTime(chat.updatedAt)}</span>
            </span>
          </button>
        {/each}
      </div>
    {/if}
  </aside>

  <div class="active-chat">
    <div class="chat-header">
      <div>
        <p class="eyebrow">{activeChat?.repoId ?? 'Workspace'}</p>
        <h2>{activeChat?.title ?? 'PMA'}</h2>
        {#if activeChat}
          <p class="chat-header-subtitle">
            {statusLabel(activeChat.status)}
            {#if activeChat.ticketId} · {activeChat.ticketId}{/if}
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

    <div class="message-stack" aria-live="polite">
      {#if loadingActive}
        <div class="state-panel">Loading active chat...</div>
      {:else if activeError}
        <div class="state-panel error">Could not load this chat. {activeError.message}</div>
      {:else if !activeChat}
        <div class="state-panel">No PMA chat is selected.</div>
      {:else if activeCards.length === 0 && approvals.length === 0}
        <div class="state-panel">This PMA chat has no visible messages yet.</div>
      {:else}
        {#each approvals as approval (approval.id)}
          <SensitiveApprovalCard {approval} onDecision={decideApproval} />
        {/each}
        {#each activeCards as card (card.id)}
          {#if card.kind === 'message'}
            <article class={`message ${card.message.role === 'user' ? 'user' : 'assistant'}`}>
              <span>{card.message.role === 'user' ? 'You' : 'PMA'}</span>
              <p>{card.message.text || 'No message text recorded.'}</p>
            </article>
          {:else if card.kind === 'ticket'}
            <article class="artifact-card ticket-card">
              <span class="artifact-type">Ticket</span>
              <strong>{card.title}</strong>
              <p>{card.summary ?? 'PMA created or is managing this ticket.'}</p>
            </article>
          {:else if card.kind === 'progress'}
            <article class="artifact-card progress-card">
              <span class="artifact-type">Run progress</span>
              <strong>{statusLabel(card.progress.status)}{card.progress.phase ? ` · ${card.progress.phase}` : ''}</strong>
              <p>{card.progress.guidance ?? `Queue depth ${card.progress.queueDepth}. Last event ${formatRelativeTime(card.progress.lastEventAt)}.`}</p>
            </article>
          {:else if card.kind === 'streaming'}
            <article class="artifact-card stream-card">
              <span class="artifact-type">Agent status</span>
              <strong>{card.progress.status === 'running' ? 'Streaming' : 'Waiting'}</strong>
              <p>
                {card.progress.elapsedSeconds ?? 0}s elapsed
                {card.progress.idleSeconds !== null ? `, ${card.progress.idleSeconds}s idle` : ''}
              </p>
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
        <button class="icon-button" type="button" aria-label="Attach files" title="Attach files" onclick={() => fileInput?.click()}>+</button>
        <button class="icon-button" type="button" aria-label="Attach images" title="Attach images" onclick={() => imageInput?.click()}>I</button>
        <button class="icon-button" type="button" aria-label="Attach link" title="Attach link" onclick={addLink}>@</button>
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
      ></textarea>
      <button class="send-button" type="submit" disabled={!activeChat || (!draft.trim() && pendingAttachments.length === 0) || sending}>
        {sending ? 'Sending' : 'Send'}
      </button>
    </form>
    <p class="permission-note">PMA has full permission for normal coding work. Sensitive CAR operations require approval.</p>
    {#if composeError}
      <p class="compose-error">{composeError.message}</p>
    {/if}
  </div>
</section>
