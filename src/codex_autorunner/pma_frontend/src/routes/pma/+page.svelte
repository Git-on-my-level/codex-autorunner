<script lang="ts">
  import { onMount } from 'svelte';
  import { pmaApi, type ApiError, type JsonRecord } from '$lib/api/client';
  import type { PmaChatMessage, PmaChatSummary, PmaRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
  import {
    buildPmaCards,
    chooseActiveChatId,
    filterPmaChats,
    formatRelativeTime,
    progressPercent,
    statusLabel,
    summarizeFilterCounts,
    type PmaCard,
    type PmaChatFilter
  } from '$lib/viewModels/pmaChat';

  let chats = $state<PmaChatSummary[]>([]);
  let messages = $state<PmaChatMessage[]>([]);
  let progress = $state<PmaRunProgress | null>(null);
  let artifacts = $state<SurfaceArtifact[]>([]);
  let agents = $state<JsonRecord[]>([]);
  let models = $state<JsonRecord[]>([]);
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
  let chatError = $state<ApiError | null>(null);
  let activeError = $state<ApiError | null>(null);
  let composeError = $state<ApiError | null>(null);

  const activeChat = $derived(chats.find((chat) => chat.id === activeChatId) ?? null);
  const filteredChats = $derived(filterPmaChats(chats, filter, search));
  const filterCounts = $derived(summarizeFilterCounts(chats));
  const activeCards = $derived<PmaCard[]>(buildPmaCards(messages, progress, activeChat, artifacts));

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
    const [chatResult, artifactResult, agentResult] = await Promise.all([
      pmaApi.pma.listChats(),
      pmaApi.pma.listFiles(),
      pmaApi.pma.listAgents()
    ]);

    if (chatResult.ok) {
      chats = chatResult.data;
      activeChatId = chooseActiveChatId(chatResult.data, activeChatId);
      if (activeChatId) void refreshActive(activeChatId);
    } else {
      chatError = chatResult.error;
    }

    if (artifactResult.ok) artifacts = artifactResult.data;
    if (agentResult.ok) {
      agents = agentResult.data;
      selectedAgent = stringField(agentResult.data[0], 'id') ?? stringField(agentResult.data[0], 'agent') ?? selectedAgent;
      void loadModels(selectedAgent);
    }
    loadingChats = false;
  }

  async function loadModels(agentId: string): Promise<void> {
    const result = await pmaApi.pma.listAgentModels(agentId);
    if (!result.ok) {
      models = [];
      selectedModel = '';
      return;
    }
    models = result.data;
    selectedModel = stringField(result.data[0], 'id') ?? stringField(result.data[0], 'model') ?? '';
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
    const message = draft.trim();
    if (!message || !activeChatId) return;
    sending = true;
    composeError = null;
    const result = await pmaApi.pma.sendMessage(activeChatId, {
      message,
      model: selectedModel || undefined,
      busy_policy: activeChat?.status === 'running' ? 'queue' : undefined
    });
    if (result.ok) {
      draft = '';
      messages = [...messages, result.data];
      await refreshActive(activeChatId, { quiet: true });
    } else {
      composeError = result.error;
    }
    sending = false;
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
        <select aria-label="PMA agent" bind:value={selectedAgent} onchange={() => loadModels(selectedAgent)}>
          {#if agents.length === 0}
            <option value={selectedAgent}>Configured agent</option>
          {:else}
            {#each agents as agent}
              <option value={stringField(agent, 'id') ?? stringField(agent, 'agent') ?? agentLabel(agent)}>
                {agentLabel(agent)}
              </option>
            {/each}
          {/if}
        </select>
        <select aria-label="Model" bind:value={selectedModel} disabled={models.length === 0}>
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
      </div>
    </div>

    <div class="message-stack" aria-live="polite">
      {#if loadingActive}
        <div class="state-panel">Loading active chat...</div>
      {:else if activeError}
        <div class="state-panel error">Could not load this chat. {activeError.message}</div>
      {:else if !activeChat}
        <div class="state-panel">No PMA chat is selected.</div>
      {:else if activeCards.length === 0}
        <div class="state-panel">This PMA chat has no visible messages yet.</div>
      {:else}
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
            <article class="artifact-card">
              <span class="artifact-type">{card.artifact.kind.replace('_', ' ')}</span>
              <strong>{card.artifact.title}</strong>
              <p>{card.artifact.summary ?? card.artifact.url ?? 'Surfaced PMA artifact.'}</p>
              {#if card.artifact.url}
                <a href={card.artifact.url}>Open artifact</a>
              {/if}
            </article>
          {/if}
        {/each}
      {/if}
    </div>

    <form class="composer" onsubmit={(event) => { event.preventDefault(); void sendMessage(); }}>
      <button class="icon-button" type="button" aria-label="Attach files" title="Attach files">+</button>
      <textarea
        aria-label="Message PMA"
        bind:value={draft}
        disabled={!activeChat || sending}
        placeholder={activeChat ? 'Message PMA about this workspace' : 'Create or select a PMA chat'}
      ></textarea>
      <button class="send-button" type="submit" disabled={!activeChat || !draft.trim() || sending}>
        {sending ? 'Sending' : 'Send'}
      </button>
    </form>
    {#if composeError}
      <p class="compose-error">{composeError.message}</p>
    {/if}
  </div>
</section>
