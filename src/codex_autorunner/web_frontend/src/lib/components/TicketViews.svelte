<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, untrack } from 'svelte';
  import type {
    TicketDetailViewModel,
    TicketFilter,
    TicketListRow,
    TicketWorkerActivity,
    TicketListViewModel
  } from '$lib/viewModels/ticket';
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import CurrentTicketChatStream from '$lib/components/CurrentTicketChatStream.svelte';
  import TicketDiffStats from '$lib/components/TicketDiffStats.svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import { noticeTone } from '$lib/noticeTone';
  import { filterTicketRows, rowRelativeTime } from '$lib/viewModels/ticket';
  import { renderMarkdownToHtml } from '$lib/viewModels/markdown';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { JsonRecord, PartialPageIssue } from '$lib/api/client';
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import { agentCanListModels, agentRecordForId } from '$lib/viewModels/modelPickers';
  import { agentIdsFromPmaAgentsPayload } from '$lib/viewModels/ticketSettingsContract';

  let {
    state: viewState,
    mode,
    list = null,
    detail = null,
    selectedFilter = 'all',
    selectedWorkspaceFilter = 'all',
    errorMessage = null,
    actionStatus = null,
    saveStatus = null,
    workerActivity = null,
    sectionIssues = [],
    agents = [],
    modelCatalogs = {},
    supportedAgentIds = [],
    onFilter = undefined,
    onRetry = undefined,
    onCommand = undefined,
    onQueueCommand = undefined,
    onReorderTicket = undefined,
    onRepairWithPma = undefined,
    onSave = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'list' | 'detail';
    list?: TicketListViewModel | null;
    detail?: TicketDetailViewModel | null;
    selectedFilter?: TicketFilter;
    selectedWorkspaceFilter?: string;
    errorMessage?: string | null;
    actionStatus?: string | null;
    saveStatus?: string | null;
    workerActivity?: TicketWorkerActivity | null;
    sectionIssues?: PartialPageIssue[];
    /** Raw GET /hub/pma/agents records so picker visibility follows capability_projection.actions.list_models. */
    agents?: JsonRecord[];
    /** GET /hub/pma/agents/{agent}/models catalogs, keyed by agent id. Null means fetch failed. */
    modelCatalogs?: Record<string, JsonRecord[] | null>;
    /** Hub GET /hub/pma/agents ids (see `agentIdsFromPmaAgentsPayload`). Fallback option added when current value is absent. */
    supportedAgentIds?: string[];
    onFilter?: ((filter: TicketFilter) => void) | undefined;
    onRetry?: (() => void) | undefined;
    onCommand?: ((command: 'resume' | 'bootstrap') => void) | undefined;
    onQueueCommand?: ((command: 'start' | 'stop' | 'restart') => void) | undefined;
    onReorderTicket?: ((sourceRouteId: string, destinationRouteId: string, placeAfter: boolean) => boolean | Promise<boolean>) | undefined;
    onRepairWithPma?: ((detail: TicketDetailViewModel) => void | Promise<void>) | undefined;
    onSave?: ((payload: TicketEditPayload) => boolean | Promise<boolean>) | undefined;
  } = $props();

  let search = $state('');
  const filteredRows = $derived(list ? filterTicketRows(list.rows, selectedFilter, selectedWorkspaceFilter) : []);
  const visibleRows = $derived.by(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return filteredRows;
    return filteredRows.filter((row) => {
      const haystack = [
        row.title,
        row.numberLabel,
        row.bodyPreview ?? '',
        row.agentLabel,
        row.modelLabel ?? '',
        row.repoLabel ?? '',
        row.pathLabel ?? ''
      ];
      return haystack.some((value) => value.toLowerCase().includes(needle));
    });
  });
  const visibleFilters = $derived(
    list ? list.filters.filter((filter) => filter.count > 0 || filter.id === selectedFilter) : []
  );
  const queueActions = $derived(list?.queueActions ?? []);
  const heroPageTitle = $derived(list ? (list.scopedOwner ? 'Tickets' : list.title) : 'Tickets');
  const heroFlowActive = $derived(
    list ? list.flowStatus.signal !== 'idle' && list.flowStatus.signal !== 'done' : false
  );
  // Only show the flow line when the queue is actively doing something.
  // The current ticket is already highlighted in the list (Working badge,
  // accent border), so a separate "Current: #N" line was duplicated signal.
  const showFlowLine = $derived.by(() => {
    if (!list?.scopedOwner) return false;
    const s = list.flowStatus.signal;
    return s !== 'idle' && s !== 'done';
  });
  const contractIssues = $derived(sectionIssues.filter((issue) => issue.id === 'ticket_contract'));
  const timelineIssues = $derived(sectionIssues.filter((issue) => issue.id === 'timeline'));
  const chatIssues = $derived(sectionIssues.filter((issue) => issue.id === 'linked_chat'));

  type TicketEditPayload = {
    title: string;
    agent: string;
    model: string;
    reasoning: string;
    done: boolean;
    frontmatterYaml?: string;
    body: string;
  };

  const initialDetail = untrack(() => detail);
  const initialSupportedAgentIds = untrack(() => supportedAgentIds);
  let editTicketId = $state<string | null>(initialDetail?.id ?? null);
  let editTitle = $state(initialDetail?.title ?? '');
  let editAgent = $state(initialDetail?.agentRaw.trim() || initialSupportedAgentIds[0] || 'codex');
  let editModel = $state(initialDetail?.modelRaw ?? '');
  let editReasoning = $state(initialDetail?.reasoningRaw ?? '');
  let editDone = $state(initialDetail?.done ?? false);
  let editFrontmatterYaml = $state(initialDetail?.frontmatterEditableYaml ?? '');
  let editBody = $state(initialDetail?.rawBody ?? '');
  let lastSettingsSignature = $state<string | null>(null);
  let settingsSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let queueOpen = $state(false);
  let repairOpen = $state(initialDetail?.needsRepair ?? false);
  let lastRepairTicketId = $state<string | null>(initialDetail?.id ?? null);
  $effect(() => {
    if (!detail) return;
    if (detail.id !== lastRepairTicketId) {
      lastRepairTicketId = detail.id;
      repairOpen = detail.needsRepair;
    }
  });
  let dragSourceRouteId = $state<string | null>(null);
  let dragSourceRowKey = $state<string | null>(null);
  let dragTargetRowKey = $state<string | null>(null);
  let dragPlaceAfter = $state(false);
  const ticketMarkdownContent = $derived(detail && editTicketId === detail.id ? editBody : detail?.rawBody ?? '');
  const newTicketHref = $derived.by(() => {
    const owner = list?.scopedOwner;
    if (!owner) return null;
    if (owner.kind === 'repo') return `/repos/${encodeURIComponent(owner.id)}/tickets/new`;
    const parent = owner.parentRepoId ?? null;
    if (parent) return `/repos/${encodeURIComponent(parent)}/worktrees/${encodeURIComponent(owner.id)}/tickets/new`;
    return null;
  });

  const agentOptions = $derived(agents.length > 0 ? agentIdsFromPmaAgentsPayload(agents) : supportedAgentIds);
  const selectedAgentRecord = $derived(agentRecordForId(agents, editAgent));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const catalogRaw = $derived(selectedAgentCanListModels ? modelCatalogs[editAgent] : undefined);
  const selectedModelCatalog = $derived(Array.isArray(catalogRaw) ? catalogRaw : []);
  const selectedModelsLoading = $derived(Boolean(selectedAgentCanListModels && catalogRaw === undefined));
  const modelCatalogError = $derived(catalogRaw === null ? 'Could not load models' : null);

  onDestroy(() => {
    if (settingsSaveTimer) clearTimeout(settingsSaveTimer);
  });

  function closeQueue(): void {
    queueOpen = false;
  }

  $effect(() => {
    if (!detail) {
      lastSettingsSignature = null;
      return;
    }
    const sig = detail.settingsSyncSignature;
    if (sig === lastSettingsSignature && editTicketId === detail.id) return;
    lastSettingsSignature = sig;
    editTicketId = detail.id;
    editTitle = detail.title;
    const fallbackAgent = agentOptions[0] ?? 'codex';
    const rawAgent = detail.agentRaw.trim();
    editAgent = rawAgent || fallbackAgent;
    editModel = detail.modelRaw;
    editReasoning = detail.reasoningRaw;
    editDone = detail.done;
    editFrontmatterYaml = detail.frontmatterEditableYaml;
    editBody = detail.rawBody;
  });

  function ticketDetailTypingTarget(target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) return false;
    const tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    return target.isContentEditable;
  }

  $effect(() => {
    if (typeof window === 'undefined') return;
    if (mode !== 'detail' || !detail) return;
    const ticketDetail = detail;
    function onKeydown(event: KeyboardEvent): void {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (ticketDetailTypingTarget(event.target)) return;
      if (event.key === '[' && ticketDetail.previousTicketHref) {
        event.preventDefault();
        void goto(href(ticketDetail.previousTicketHref));
      } else if (event.key === ']' && ticketDetail.nextTicketHref) {
        event.preventDefault();
        void goto(href(ticketDetail.nextTicketHref));
      }
    }
    window.addEventListener('keydown', onKeydown);
    return () => window.removeEventListener('keydown', onKeydown);
  });

  function scheduleSettingsSave(delay = 450): void {
    if (!onSave) return;
    if (settingsSaveTimer) clearTimeout(settingsSaveTimer);
    settingsSaveTimer = setTimeout(() => {
      settingsSaveTimer = null;
      void saveSettings();
    }, delay);
  }

  async function saveSettings(): Promise<boolean> {
    if (!onSave) return false;
    return Boolean(await onSave({ title: editTitle, agent: editAgent, model: editModel, reasoning: editReasoning, done: editDone, frontmatterYaml: editFrontmatterYaml, body: editBody }));
  }

  async function saveMarkdown(_docId: string, content: string): Promise<boolean> {
    editBody = content;
    return Boolean(await onSave?.({ title: editTitle, agent: editAgent, model: editModel, reasoning: editReasoning, done: editDone, frontmatterYaml: editFrontmatterYaml, body: content }));
  }

  function routeNumber(routeId: string): number | null {
    const value = Number(routeId);
    return Number.isInteger(value) ? value : null;
  }

  function canDragTicketRow(row: TicketListRow): boolean {
    if (!onReorderTicket || routeNumber(row.routeId) === null) return false;
    if (row.status === 'invalid') return false;
    const owner = list?.scopedOwner;
    if (!owner) return false;
    return row.workspaceKind === owner.kind && row.workspaceId === owner.id;
  }

  function ticketDragDisabledReason(row: TicketListRow): string {
    if (routeNumber(row.routeId) === null) return 'Only numbered tickets can be reordered';
    if (row.status === 'invalid') return 'Fix duplicate or invalid ticket metadata before reordering';
    const owner = list?.scopedOwner;
    if (owner && (row.workspaceKind !== owner.kind || row.workspaceId !== owner.id)) {
      return row.workspaceKind === 'worktree'
        ? 'Open this worktree queue to reorder this ticket'
        : 'Open the owning queue to reorder this ticket';
    }
    return 'Ticket reordering is unavailable';
  }

  function rowDragKey(row: TicketListRow): string {
    return row.id;
  }

  function resetTicketDrag(): void {
    dragSourceRouteId = null;
    dragSourceRowKey = null;
    dragTargetRowKey = null;
    dragPlaceAfter = false;
  }

  function beginTicketDrag(event: DragEvent, row: TicketListRow): void {
    if (!canDragTicketRow(row)) {
      event.preventDefault();
      return;
    }
    dragSourceRouteId = row.routeId;
    dragSourceRowKey = rowDragKey(row);
    dragTargetRowKey = rowDragKey(row);
    dragPlaceAfter = false;
    event.dataTransfer?.setData('text/plain', rowDragKey(row));
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function updateTicketDropTarget(event: DragEvent, row: TicketListRow): void {
    if (!dragSourceRouteId || !canDragTicketRow(row)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    dragTargetRowKey = rowDragKey(row);
    dragPlaceAfter = event.clientY > rect.top + rect.height / 2;
  }

  async function dropTicketRow(event: DragEvent, row: TicketListRow): Promise<void> {
    if (!dragSourceRouteId || !canDragTicketRow(row)) return;
    event.preventDefault();
    const sourceRouteId = dragSourceRouteId;
    const destinationRouteId = row.routeId;
    const placeAfter = dragPlaceAfter;
    resetTicketDrag();
    if (sourceRouteId === destinationRouteId) return;
    await onReorderTicket?.(sourceRouteId, destinationRouteId, placeAfter);
  }

  function queueActionLabel(action: 'start' | 'stop' | 'restart'): string {
    return queueActions.find((candidate) => candidate.action === action)?.label ?? (action === 'start' ? 'Start queue' : action === 'stop' ? 'Stop queue' : 'Restart queue');
  }

  function queueActionEnabled(action: 'start' | 'stop' | 'restart'): boolean {
    return queueActions.find((candidate) => candidate.action === action)?.enabled === true;
  }

  function queueActionReason(action: 'start' | 'stop' | 'restart'): string | null {
    return queueActions.find((candidate) => candidate.action === action)?.disabledReason ?? null;
  }

  function ticketTranscriptStartsOpen(status: string): boolean {
    return status === 'running' || status === 'waiting' || status === 'blocked';
  }
</script>

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading tickets...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load tickets. {errorMessage}</div>
  </section>
{:else if mode === 'list' && list}
  <section class="page-stack ticket-page">
    <PageHero title={heroPageTitle}>
      {#snippet stats()}
        {#if list.scopedOwner && heroFlowActive}
          <span class={`hero-queue-pill status-pill ${list.flowStatus.signal}`} aria-label="Queue status" title="Ticket queue status">
            {list.flowStatus.statusLabel}
          </span>
        {/if}
      {/snippet}
      {#snippet actions()}
        {#if list.scopedOwner}
          <div class="ticket-queue-actions" role="group" aria-label="Ticket flow controls">
            {#each (['start', 'stop', 'restart'] as const) as action}
              {#if queueActionEnabled(action)}
                <button
                  type="button"
                  class={action === 'start' ? 'primary-button hero-action' : 'ghost-button hero-action'}
                  title={queueActionReason(action)}
                  onclick={() => onQueueCommand?.(action)}
                >
                  {queueActionLabel(action)}
                </button>
              {/if}
            {/each}
          </div>
        {/if}
        {#if newTicketHref}
          <a class="ghost-button hero-action" href={href(newTicketHref)} data-sveltekit-preload-data="tap">+ New ticket</a>
        {/if}
      {/snippet}
    </PageHero>

    {#if list.currentChatId && heroFlowActive}
      <CurrentTicketChatStream
        chatId={list.currentChatId}
        ticketLabel={list.flowStatus.currentTicketLabel !== 'None' ? list.flowStatus.currentTicketLabel : null}
        ticketHref={list.flowStatus.currentTicketHref}
        statusLabel={list.flowStatus.statusLabel}
        statusSignal={list.flowStatus.signal}
      />
    {/if}

    <header class="ticket-controls">
      <label class="search-field ticket-search">
        <span class="sr-only">Search tickets</span>
        <input bind:value={search} type="search" placeholder="Search tickets, body, agent" />
      </label>
      <div class="filter-row ticket-filter-row" role="tablist" aria-label="Ticket filters">
        {#each visibleFilters as filter}
          <button
            class:active={selectedFilter === filter.id}
            class="chip"
            type="button"
            role="tab"
            aria-selected={selectedFilter === filter.id}
            onclick={() => onFilter?.(filter.id)}
          >
            {filter.label}
            <span>{filter.count}</span>
          </button>
        {/each}
      </div>
    </header>

    <AutoDismissNotice message={actionStatus} tone={noticeTone(actionStatus)} />

    <section class="ticket-list-section">
      {#if showFlowLine}
        <p class={`ticket-flow-line ${list.flowStatus.signal}`} aria-label="Ticket flow status">
          <span class="status-pill {list.flowStatus.signal}">{list.flowStatus.statusLabel}</span>
          {#if list.flowStatus.currentTicketLabel !== 'None'}
            <span class="flow-line-current">
              Current:
              {#if list.flowStatus.currentTicketHref}
                <a href={href(list.flowStatus.currentTicketHref)}>{list.flowStatus.currentTicketLabel}</a>
              {:else}
                {list.flowStatus.currentTicketLabel}
              {/if}
            </span>
          {/if}
          {#if list.flowStatus.turnsLabel !== 'Unknown'}
            <span class="flow-line-meta">{list.flowStatus.turnsLabel} turns</span>
          {/if}
          {#if list.flowStatus.elapsedLabel !== 'Unknown'}
            <span class="flow-line-meta">{list.flowStatus.elapsedLabel}</span>
          {/if}
          {#if list.flowStatus.lastActivityLabel && list.flowStatus.lastActivityLabel !== 'No activity yet'}
            <span class="flow-line-meta">last activity {list.flowStatus.lastActivityLabel}</span>
          {/if}
          {#if list.flowStatus.reasonLabel && list.flowStatus.reasonLabel !== 'No reason reported'}
            <span class="flow-line-meta flow-line-reason">— {list.flowStatus.reasonLabel}</span>
          {/if}
        </p>
      {/if}
      {@render degradedIssues(timelineIssues)}
      {@render degradedIssues(chatIssues)}
      {#if visibleRows.length === 0}
        <div class="state-panel empty-state compact-empty">
          {#if search.trim()}
            <strong>No matches</strong>
            <p>Clear the search or pick a different filter.</p>
          {:else}
            <strong>No tickets in this view</strong>
            <p>Switch filters or ask PMA to create the next scoped ticket for the current CAR work.</p>
          {/if}
        </div>
      {:else}
        <div class="ticket-card-list" role="list" aria-label="Ticket queue">
          {#each visibleRows as row}
            {@const numberDigits = row.numberLabel.replace(/^#/, '')}
            {@const hasNumber = numberDigits !== row.numberLabel}
            {@const agentText = row.agentLabel && row.agentLabel !== 'Unassigned' ? row.agentLabel : null}
            <article
              class={`ticket-card ${row.status} ${dragSourceRowKey === rowDragKey(row) ? 'drag-source' : ''} ${dragTargetRowKey === rowDragKey(row) && !dragPlaceAfter && dragSourceRowKey !== rowDragKey(row) ? 'drop-before' : ''} ${dragTargetRowKey === rowDragKey(row) && dragPlaceAfter && dragSourceRowKey !== rowDragKey(row) ? 'drop-after' : ''}`}
              class:current={row.isCurrent}
              class:done={row.status === 'done'}
              role="listitem"
              ondragover={(event) => updateTicketDropTarget(event, row)}
              ondrop={(event) => void dropTicketRow(event, row)}
            >
              <button
                type="button"
                class="ticket-drag-handle"
                draggable={canDragTicketRow(row)}
                disabled={!canDragTicketRow(row)}
                aria-label={`Drag ${row.numberLabel} to reorder`}
                title={canDragTicketRow(row) ? 'Drag to reorder in this queue' : ticketDragDisabledReason(row)}
                ondragstart={(event) => beginTicketDrag(event, row)}
                ondragend={resetTicketDrag}
              >
                <span aria-hidden="true">☰</span>
              </button>
              <a class="ticket-card-body" href={href(row.href)} data-sveltekit-preload-data="tap">
                <span class="ticket-card-num" aria-label={hasNumber ? `Ticket ${numberDigits}` : row.numberLabel}>
                  {#if hasNumber}
                    <span class="ticket-card-num-hash" aria-hidden="true">#</span><span class="ticket-card-num-value">{numberDigits}</span>
                  {:else}
                    <span class="ticket-card-num-value">{row.numberLabel}</span>
                  {/if}
                </span>
                <div class="ticket-card-main">
                  <div class="ticket-card-title-row">
                    <strong class="ticket-card-title">{row.title}</strong>
                    {#if row.isCurrent && row.status === 'idle'}<em class="working-badge">Working</em>{/if}
                    {#if row.workspaceKind === 'unscoped'}<em class="working-badge needs-repair" title="Needs owner repair">Needs owner repair</em>{/if}
                    {#if row.status !== 'idle'}
                      <span class="status-pill {row.status}">{statusLabel(row.status)}</span>
                    {/if}
                    {#if row.currentRunState && row.currentRunState !== 'idle' && row.currentRunState !== 'done' && row.currentRunState !== row.status}
                      <span class="status-pill {row.currentRunState}" title="Run status">{statusLabel(row.currentRunState)}</span>
                    {/if}
                  </div>
                  <div class="ticket-card-meta">
                    {#if row.bodyPreview}<span class="ticket-card-preview">{row.bodyPreview}</span>{/if}
                    <TicketDiffStats stats={row.diffStats} />
                    {#if row.durationLabel}<span>{row.durationLabel}</span>{/if}
                    {#if row.updatedAt}<span>{rowRelativeTime(row)}</span>{/if}
                  </div>
                </div>
                {#if agentText || row.modelLabel}
                  <div class="ticket-card-side" aria-label="Agent and model">
                    {#if agentText}
                      <span class="ticket-card-agent">{agentText}</span>
                    {/if}
                    {#if row.modelLabel}
                      <span class="ticket-card-model">{row.modelLabel}</span>
                    {/if}
                  </div>
                {/if}
              </a>
            </article>
          {/each}
        </div>
      {/if}
    </section>
  </section>
{:else if mode === 'detail' && detail}
  {@const ownerLabelKind = detail.workspaceKind === 'repo' ? 'repo' : detail.workspaceKind === 'worktree' ? 'worktree' : 'workspace'}
  {@const queueLabel = detail.workspaceKind === 'repo' ? 'Repo tickets' : detail.workspaceKind === 'worktree' ? 'Worktree tickets' : 'Tickets'}
  {@const primaryActions = detail.actions.filter((action) => !action.secondary)}
  <section class="page-stack ticket-page ticket-detail-page">
    <header class="ticket-hero ticket-hero-flat">
      <div class="ticket-hero-row">
        <h1 class="ticket-hero-title">
          <span class="ticket-hero-num">{detail.numberLabel}</span>
          <span class="ticket-hero-title-text">
            <input
              class="ticket-title-input"
              bind:value={editTitle}
              oninput={() => scheduleSettingsSave()}
              aria-label="Ticket title"
              placeholder="Untitled ticket"
            />
          </span>
        </h1>
      </div>

      <div class="ticket-settings-bar" aria-label="Ticket settings">
        {#if detail.needsRepair}
          <button
            type="button"
            class="status-pill invalid status-pill-button"
            aria-expanded={repairOpen}
            aria-controls="ticket-repair-panel"
            title="Show what needs repair"
            onclick={() => (repairOpen = !repairOpen)}
          >{statusLabel('invalid')}</button>
        {:else if detail.status !== 'idle'}
          <span class="status-pill {detail.status}">{statusLabel(detail.status)}</span>
        {/if}
        <AgentModelReasoningPicker
          variant="ticket"
          {agents}
          fallbackAgentIds={agentOptions}
          enableHermesProfile={false}
          bind:agentValue={editAgent}
          bind:modelValue={editModel}
          bind:reasoningValue={editReasoning}
          models={selectedModelCatalog}
          loading={selectedModelsLoading}
          modelCatalogError={modelCatalogError}
          allowEmptyModelOption={true}
          onchange={() => scheduleSettingsSave(0)}
        />
        <label class="ticket-inline-field ticket-inline-done">
          <input type="checkbox" bind:checked={editDone} onchange={() => scheduleSettingsSave(0)} />
          <span>Done</span>
        </label>
      </div>

      <div class="ticket-hero-footer" aria-label="Ticket navigation and metadata">
        <div class="ticket-hero-controls">
          {#if detail.sourceTickets.length > 0}
            <button
              type="button"
              class="ghost-button"
              aria-expanded={queueOpen}
              aria-controls="ticket-queue-drawer"
              onclick={() => (queueOpen = !queueOpen)}
            >
              <span aria-hidden="true">☰</span> Queue
              <span class="muted">({detail.sourceTickets.length})</span>
            </button>
          {/if}
          {#if detail.previousTicketHref}
            <a class="ghost-button ticket-nav-btn" href={href(detail.previousTicketHref)} aria-label="Previous ticket">
              <span class="kbd">[</span>
              <span>‹ Prev</span>
            </a>
          {/if}
          {#if detail.nextTicketHref}
            <a class="ghost-button ticket-nav-btn" href={href(detail.nextTicketHref)} aria-label="Next ticket">
              <span>Next ›</span>
              <span class="kbd">]</span>
            </a>
          {/if}
        </div>
        <div class="ticket-hero-meta">
          {#if detail.updatedLabel && detail.updatedLabel !== '—'}
            {@const hasActivity = detail.updatedLabel !== 'No activity yet'}
            <span class="ticket-inline-meta" class:muted={!hasActivity} title={hasActivity ? 'Last updated' : 'No run activity yet'}>
              {hasActivity ? `Updated ${detail.updatedLabel}` : 'Not started'}
            </span>
          {/if}
          {#if detail.pathLabel}
            <span class="ticket-inline-path" title={detail.pathLabel}>{detail.pathLabel}</span>
          {/if}
        </div>
      </div>

      {#if detail.linkedChats.length > 0}
        <div class="ticket-chats-strip" aria-label="Chats spawned for this ticket">
          <span class="ticket-chats-label">Chats</span>
          <div class="ticket-chats-pills">
            {#each detail.linkedChats as chat (chat.id)}
              <a class={`ticket-chat-pill status-${chat.status}`} href={href(chat.href)} title={`Open chat · ${chat.title}`}>
                <span class={`status-dot status-${chat.status}`} aria-hidden="true"></span>
                <span class="ticket-chat-pill-agent">{chat.agentId ?? chat.kindLabel}</span>
                {#if chat.status !== 'idle' && chat.status !== 'done'}
                  <span class="ticket-chat-pill-status">{statusLabel(chat.status)}</span>
                {/if}
                <span class="ticket-chat-pill-arrow" aria-hidden="true">→</span>
              </a>
            {/each}
          </div>
        </div>
      {/if}

      {#if detail.needsRepair && repairOpen}
        <div id="ticket-repair-panel" class="ticket-repair-banner" role="region" aria-label="Ticket repair details">
          <div class="ticket-repair-header">
            <strong>This ticket needs repair before it can run.</strong>
            <div class="ticket-repair-actions">
              {#if onRepairWithPma}
                <button type="button" class="ghost-button" onclick={() => onRepairWithPma?.(detail)}>Fix with PMA</button>
              {/if}
              <button type="button" class="ghost-button ticket-repair-close" onclick={() => (repairOpen = false)} aria-label="Close repair panel">Dismiss</button>
            </div>
          </div>
          {#if detail.errors.length > 0}
            <ul class="ticket-repair-errors">
              {#each detail.errors as err}<li>{err}</li>{/each}
            </ul>
          {:else}
            <p class="ticket-repair-hint">Frontmatter validation failed. Fix the keys below, then save.</p>
          {/if}
          <details class="ticket-repair-frontmatter" open>
            <summary>Frontmatter</summary>
            <label class="ticket-frontmatter-editor">
              <span class="sr-only">Ticket frontmatter YAML</span>
              <textarea
                bind:value={editFrontmatterYaml}
                rows="7"
                spellcheck="false"
                aria-label="Ticket frontmatter YAML"
                placeholder={'agent: codex\ndone: false'}
              ></textarea>
            </label>
            <div class="ticket-frontmatter-controls">
              <button type="button" class="primary-button" disabled={!onSave} onclick={() => void saveSettings()}>Save frontmatter</button>
              <span class="ticket-repair-hint">Edit the YAML block exactly as it should appear between <code>---</code> delimiters.</span>
            </div>
            {#if detail.pathLabel}
              <p class="ticket-repair-path">Ticket file: <code>{detail.pathLabel}</code>. Body edits below save with this frontmatter.</p>
            {/if}
          </details>
        </div>
      {/if}

      {#if primaryActions.length > 0}
        <div class="ticket-hero-actions">
          {#each primaryActions as action}
            {#if action.command}
              {@const command = action.command}
              <button type="button" class="primary-button" onclick={() => onCommand?.(command)}>{action.label}</button>
            {:else if action.href}
              <a class="primary-button" href={href(action.href)}>{action.label}</a>
            {/if}
          {/each}
        </div>
      {/if}
    </header>

    <AutoDismissNotice message={actionStatus} tone={noticeTone(actionStatus)} />
    <AutoDismissNotice message={saveStatus} tone={noticeTone(saveStatus)} />

    {@render degradedIssues(contractIssues)}

    <div class="ticket-detail-layout ticket-detail-single">
      <main class="ticket-main-column">
        <article class="ticket-markdown-card ticket-markdown-flat">
          <EditableMarkdown
            docId={detail.id}
            content={ticketMarkdownContent}
            html={renderMarkdownToHtml(ticketMarkdownContent)}
            isMissing={!ticketMarkdownContent.trim()}
            emptyTitle="No description"
            emptyMessage="Add the ticket goal, tasks, acceptance criteria, or notes."
            editable={Boolean(onSave)}
            onSave={saveMarkdown}
          />
        </article>

        {#if workerActivity && workerActivity.items.length > 0}
          <section class="ticket-flow-section" aria-label="Worker output">
            <h2 class="ticket-section-heading">Worker output</h2>
            <details class="ticket-worker-output" open={ticketTranscriptStartsOpen(detail.status)}>
              <summary>
                <span>Live stream</span>
                <span class="muted">{workerActivity.items.length} item{workerActivity.items.length === 1 ? '' : 's'}</span>
              </summary>
              <div class="ticket-worker-output-list">
                {#each workerActivity.items as item}
                  <article class={`ticket-worker-output-item ${item.status}`}>
                    <div>
                      <strong>{item.title}</strong>
                      {#if item.timestamp}<span>{rowRelativeTime({ updatedAt: item.timestamp })}</span>{/if}
                    </div>
                    {#if item.summary}<p>{item.summary}</p>{/if}
                    {#if item.detail}<pre>{item.detail}</pre>{/if}
                  </article>
                {/each}
              </div>
            </details>
          </section>
        {/if}
      </main>
    </div>

    <div class="secondary-actions">
      {#if detail.ownerTicketListHref}
        <a href={href(detail.ownerTicketListHref)}>Back to {ownerLabelKind} tickets</a>
      {/if}
      {#each detail.actions.filter((action) => action.secondary) as action}
        {#if action.href}<a href={href(action.href)}>{action.label}</a>{/if}
      {/each}
    </div>

    {#if queueOpen}
      <div
        class="ticket-queue-overlay"
        role="presentation"
        onclick={closeQueue}
        onkeydown={(event) => event.key === 'Escape' && closeQueue()}
      ></div>
      <aside id="ticket-queue-drawer" class="ticket-queue-drawer page-panel" aria-label={`${queueLabel} queue`}>
        <div class="panel-heading-row">
          <h2>{queueLabel}</h2>
          <button type="button" class="ghost-button" onclick={closeQueue} aria-label="Close queue">Close</button>
        </div>
        <div class="ticket-nav-list">
          {#each detail.sourceTickets as row}
            <a
              class={`ticket-nav-row ${row.status}`}
              class:active={row.routeId === detail.routeId || row.id === detail.id}
              href={href(row.href)}
              data-sveltekit-preload-data="tap"
              onclick={closeQueue}
            >
              <span>
                <strong>{row.numberLabel}</strong>
                <span>{row.title}</span>
              </span>
              <span>
                <em>{statusLabel(row.status)}</em>
                <TicketDiffStats stats={row.diffStats} />
              </span>
            </a>
          {/each}
        </div>
        {#if detail.ownerTicketListHref}
          <a class="ticket-queue-footer-link" href={href(detail.ownerTicketListHref)}>View full queue →</a>
        {/if}
      </aside>
    {/if}
  </section>
{/if}

{#snippet degradedIssues(issues: PartialPageIssue[])}
  {#each issues as issue}
    <div class="state-panel degraded-state">
      <strong>{issue.title}</strong>
      <p>{issue.message}</p>
      {#if onRetry}<button type="button" onclick={() => onRetry?.()}>{issue.retryLabel}</button>{/if}
    </div>
  {/each}
{/snippet}

<style>
  .ticket-controls {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: 0 2px;
  }
  .ticket-search {
    flex: 1 1 auto;
    min-width: 0;
  }
  @media (max-width: 760px) {
    .ticket-controls {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
      padding: 0;
    }
  }

  .ticket-list-section {
    display: grid;
    gap: var(--space-3);
  }

  .ticket-card-list {
    display: grid;
    gap: var(--space-2);
  }

  .ticket-queue-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .ticket-card {
    display: grid;
    grid-template-columns: 24px minmax(0, 1fr);
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
    transition: border-color var(--transition-base), background var(--transition-base);
  }

  .ticket-card:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-sunken, var(--color-surface));
  }

  .ticket-card.waiting,
  .ticket-card.blocked,
  .ticket-card.invalid {
    border-left: 3px solid var(--color-warning);
  }

  .ticket-card.failed {
    border-left: 3px solid var(--color-danger);
  }

  .ticket-card.current {
    border-color: color-mix(in srgb, var(--color-warning) 48%, var(--color-border-subtle));
    background: color-mix(in srgb, var(--color-warning) 10%, var(--color-surface));
  }

  .ticket-card.current:hover {
    border-color: color-mix(in srgb, var(--color-warning) 55%, var(--color-border-strong));
    background: color-mix(in srgb, var(--color-warning) 14%, var(--color-surface-sunken, var(--color-surface)));
  }

  .ticket-card.done {
    border-color: color-mix(in srgb, var(--color-success) 38%, var(--color-border-subtle));
    background: var(--color-success-soft);
  }

  .ticket-card.done:hover {
    border-color: color-mix(in srgb, var(--color-success) 48%, var(--color-border-strong));
    background: color-mix(in srgb, var(--color-success-soft) 92%, var(--color-surface-sunken, var(--color-surface)));
  }

  .ticket-card.drag-source {
    opacity: 0.45;
  }

  .ticket-card.drop-before {
    box-shadow: inset 0 3px 0 var(--color-accent);
    border-color: var(--color-accent);
  }

  .ticket-card.drop-after {
    box-shadow: inset 0 -3px 0 var(--color-accent);
    border-color: var(--color-accent);
  }

  .ticket-drag-handle {
    width: 24px;
    height: 24px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: grab;
    padding: 0;
    border-radius: 4px;
  }
  .ticket-drag-handle:disabled {
    visibility: hidden;
  }
  .ticket-drag-handle:hover:not(:disabled) {
    color: var(--color-ink);
    background: var(--color-surface-sunken, transparent);
  }

  .ticket-card-body {
    display: grid;
    grid-template-columns: 4.5ch minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
    text-decoration: none;
    color: inherit;
  }

  .ticket-card-num {
    display: inline-flex;
    align-items: baseline;
    justify-content: flex-end;
    gap: 0;
    color: var(--color-accent);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
    white-space: nowrap;
  }
  .ticket-card-num-hash {
    color: var(--color-ink-faint);
    font-weight: 500;
    margin-right: 1px;
  }
  .ticket-card-num-value {
    color: var(--color-accent);
  }

  .ticket-card-side {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
    white-space: nowrap;
    text-align: right;
    flex-shrink: 0;
    font-size: var(--font-size-0);
    line-height: 1.25;
  }

  .ticket-card-agent {
    color: var(--color-ink-soft);
    font-weight: 500;
  }

  .ticket-card-model {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: 11px;
    color: var(--color-ink-faint);
    letter-spacing: 0.01em;
    max-width: 14rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  @media (max-width: 640px) {
    .ticket-card-body {
      grid-template-columns: 4.5ch minmax(0, 1fr);
    }
    .ticket-card-side {
      grid-column: 2 / -1;
      flex-direction: row;
      justify-content: flex-start;
      align-items: baseline;
      gap: var(--space-2);
    }
    .ticket-card-model {
      max-width: none;
    }
  }

  .ticket-card-main {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .ticket-card-title-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }

  .ticket-card-title {
    color: var(--color-ink);
    font-weight: 600;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ticket-card-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0 var(--space-2);
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    min-width: 0;
  }
  .ticket-card-meta > span {
    min-width: 0;
  }
  .ticket-card-meta > span + span::before {
    content: '·';
    margin-right: var(--space-2);
    color: var(--color-border-strong);
  }
  .ticket-card-preview {
    color: var(--color-ink-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 200px;
  }
  .ticket-card-meta :global(.ticket-diff-stats) {
    font-variant-numeric: tabular-nums;
  }

  .ticket-detail-single.ticket-detail-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  @media (min-width: 1280px) {
    .ticket-detail-single.ticket-detail-layout {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-4);
    }

    .ticket-detail-single .ticket-main-column {
      max-width: none;
      margin-inline: 0;
    }
  }

  .ticket-nav-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .kbd {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: var(--font-size-0);
    font-variant-numeric: tabular-nums;
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    padding: 1px 6px;
    line-height: 1.35;
  }

  /* Flatten the hero — no card border, no shadow, denser. */
  :global(.ticket-detail-page .ticket-hero.ticket-hero-flat) {
    padding: 0;
    background: transparent;
    border: none;
    box-shadow: none;
    gap: var(--space-3);
  }

  .ticket-hero-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .ticket-hero-title {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    flex: 1 1 auto;
    min-width: 0;
  }

  .ticket-hero-num {
    color: var(--color-accent);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .ticket-hero-title-text {
    flex: 1 1 auto;
    min-width: 0;
  }

  .ticket-title-input {
    width: 100%;
    background: transparent;
    border: 1px solid transparent;
    padding: 2px 8px;
    margin-left: -8px;
    border-radius: 6px;
    color: var(--color-ink);
    font-size: var(--font-size-5);
    line-height: 1.18;
    letter-spacing: -0.022em;
    font-weight: 650;
    transition: background var(--transition-base), border-color var(--transition-base);
  }
  .ticket-title-input:hover {
    background: var(--color-surface-muted);
  }
  .ticket-title-input:focus {
    outline: none;
    background: var(--color-surface);
    border-color: var(--color-border);
  }

  .ticket-settings-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) 0;
    border-top: 1px solid var(--color-border-subtle);
    border-bottom: 1px solid var(--color-border-subtle);
  }

  .ticket-settings-bar :global(.status-pill) {
    align-self: center;
    padding: 2px 8px;
    line-height: 1.4;
  }

  .status-pill-button {
    border: none;
    cursor: pointer;
    font-family: inherit;
  }
  .status-pill-button:hover,
  .status-pill-button:focus-visible {
    filter: brightness(1.08);
    outline: none;
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-warning) 35%, transparent);
  }

  .ticket-inline-meta.muted {
    color: var(--color-ink-faint);
    font-style: italic;
  }

  .ticket-repair-banner {
    display: grid;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid color-mix(in srgb, var(--color-warning) 40%, var(--color-border-subtle));
    border-radius: 8px;
    background: color-mix(in srgb, var(--color-warning) 8%, var(--color-surface));
  }
  .ticket-repair-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .ticket-repair-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }
  .ticket-repair-close {
    flex-shrink: 0;
  }
  .ticket-repair-errors {
    margin: 0;
    padding-left: var(--space-4);
    color: var(--color-warning);
    font-size: var(--font-size-0);
    display: grid;
    gap: 2px;
  }
  .ticket-repair-hint {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }
  .ticket-repair-frontmatter > summary {
    cursor: pointer;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    padding: 2px 0;
  }
  .ticket-frontmatter-editor {
    display: block;
    margin-top: var(--space-2);
  }
  .ticket-frontmatter-editor textarea {
    width: 100%;
    min-height: 8rem;
    resize: vertical;
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    color: var(--color-ink);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: var(--font-size-0);
    line-height: 1.5;
  }
  .ticket-frontmatter-editor textarea:focus {
    outline: none;
    border-color: var(--color-border);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent) 18%, transparent);
  }
  .ticket-frontmatter-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }
  .ticket-repair-frontmatter pre {
    margin: var(--space-2) 0 0;
    padding: var(--space-2) var(--space-3);
    background: var(--color-surface-muted);
    border: 1px solid var(--color-border-subtle);
    border-radius: 6px;
    overflow-x: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: var(--font-size-0);
    line-height: 1.5;
    color: var(--color-ink);
  }
  .ticket-repair-path {
    margin: var(--space-2) 0 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }
  .ticket-repair-path code,
  .ticket-repair-hint code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, 'Courier New', monospace;
    font-size: 12px;
    background: var(--color-surface-muted);
    padding: 1px 4px;
    border-radius: 4px;
  }

  .ticket-settings-bar :global(.ticket-inline-field) {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin: 0;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
  }
  .ticket-settings-bar :global(.ticket-inline-field > span) {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    font-size: 10px;
  }
  .ticket-settings-bar :global(.ticket-inline-field :is(input:not([type='checkbox']), select)) {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 6px;
    color: var(--color-ink);
    font-size: var(--font-size-0);
    min-width: 0;
    max-width: 12rem;
  }
  .ticket-settings-bar :global(.ticket-inline-field :is(input:not([type='checkbox']), select):hover) {
    background: var(--color-surface-muted);
  }
  .ticket-settings-bar :global(.ticket-inline-field :is(input:not([type='checkbox']), select):focus) {
    outline: none;
    background: var(--color-surface);
    border-color: var(--color-border);
  }
  .ticket-inline-done {
    gap: 4px;
  }
  .ticket-inline-done input[type='checkbox'] {
    margin: 0;
  }
  .ticket-inline-done > span {
    color: var(--color-ink);
    text-transform: none;
    letter-spacing: normal;
    font-weight: 500;
    font-size: var(--font-size-0);
  }

  .ticket-inline-meta {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
  }

  .ticket-hero-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    row-gap: var(--space-2);
  }
  .ticket-hero-footer .ticket-hero-controls {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .ticket-hero-meta {
    display: inline-flex;
    align-items: center;
    gap: var(--space-3);
    margin-left: auto;
    min-width: 0;
  }
  @media (max-width: 760px) {
    .ticket-hero-meta {
      margin-left: 0;
      width: 100%;
    }
  }

  .ticket-chats-strip {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }
  .ticket-chats-label {
    font-size: var(--font-size-0);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--color-ink-muted);
    font-weight: 600;
  }
  .ticket-chats-pills {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .ticket-chat-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 999px;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: var(--font-size-0);
    font-weight: 550;
    text-decoration: none;
    transition: border-color var(--transition-fast), background-color var(--transition-fast), color var(--transition-fast);
  }
  .ticket-chat-pill:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }
  .ticket-chat-pill .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }
  .ticket-chat-pill-status {
    color: var(--color-ink-muted);
    font-size: 11px;
    text-transform: lowercase;
  }
  .ticket-chat-pill-arrow {
    color: var(--color-ink-faint);
    font-size: 11px;
    transition: transform var(--transition-fast), color var(--transition-fast);
  }
  .ticket-chat-pill:hover .ticket-chat-pill-arrow {
    color: var(--color-ink-soft);
    transform: translateX(2px);
  }

  .ticket-inline-path {
    color: var(--color-ink-faint);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-0);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 24rem;
  }

  /* Markdown body without the card chrome */
  :global(.ticket-detail-page .ticket-markdown-card.ticket-markdown-flat) {
    padding: 0;
    background: transparent;
    border: none;
    box-shadow: none;
  }

  .ticket-section-heading {
    margin: 0 0 var(--space-2);
    font-size: var(--font-size-2);
    font-weight: 600;
    color: var(--color-ink);
    letter-spacing: -0.01em;
  }

  .ticket-flow-section {
    display: grid;
    gap: var(--space-2);
  }

  .ticket-flow-timeline {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-2);
  }

  .ticket-flow-timeline > li {
    margin: 0;
  }

  .ticket-flow-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--space-3);
    align-items: center;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
    text-decoration: none;
    color: inherit;
    transition: border-color var(--transition-fast), background var(--transition-fast);
  }

  a.ticket-flow-row:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-sunken, var(--color-surface));
  }

  .ticket-flow-body {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .ticket-flow-body strong {
    font-size: var(--font-size-1);
    font-weight: 600;
    letter-spacing: -0.005em;
  }

  .ticket-flow-summary {
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    line-height: 1.45;
  }

  .ticket-flow-time {
    font-size: var(--font-size-0);
    color: var(--color-ink-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .ticket-worker-output {
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
  }
  .ticket-worker-output > summary {
    padding: var(--space-2) var(--space-3);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: 600;
  }
</style>
