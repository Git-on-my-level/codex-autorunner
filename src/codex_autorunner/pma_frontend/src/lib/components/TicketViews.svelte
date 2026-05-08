<script lang="ts">
  import { onDestroy } from 'svelte';
  import type {
    TicketDetailViewModel,
    TicketFilter,
    TicketListRow,
    TicketListViewModel
  } from '$lib/viewModels/ticket';
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
  import PageHero from '$lib/components/PageHero.svelte';
  import PmaTranscriptCards from '$lib/components/PmaTranscriptCards.svelte';
  import { filterTicketRows, rowRelativeTime } from '$lib/viewModels/ticket';
  import { renderMarkdownToHtml } from '$lib/viewModels/markdown';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { PartialPageIssue } from '$lib/api/client';

  let {
    state: viewState,
    mode,
    list = null,
    detail = null,
    selectedFilter = 'needs_attention',
    selectedWorkspaceFilter = 'all',
    errorMessage = null,
    actionStatus = null,
    saveStatus = null,
    sectionIssues = [],
    onFilter = undefined,
    onRetry = undefined,
    onCommand = undefined,
    onQueueCommand = undefined,
    onCreateTicket = undefined,
    onReorderTicket = undefined,
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
    sectionIssues?: PartialPageIssue[];
    onFilter?: ((filter: TicketFilter) => void) | undefined;
    onRetry?: (() => void) | undefined;
    onCommand?: ((command: 'resume' | 'bootstrap') => void) | undefined;
    onQueueCommand?: ((command: 'start' | 'stop' | 'restart') => void) | undefined;
    onCreateTicket?: ((payload: TicketCreatePayload) => boolean | Promise<boolean>) | undefined;
    onReorderTicket?: ((sourceRouteId: string, destinationRouteId: string, placeAfter: boolean) => boolean | Promise<boolean>) | undefined;
    onSave?: ((payload: TicketEditPayload) => boolean | Promise<boolean>) | undefined;
  } = $props();

  const visibleRows = $derived(list ? filterTicketRows(list.rows, selectedFilter, selectedWorkspaceFilter) : []);
  const queueActions = $derived(list?.queueActions ?? []);
  const contractIssues = $derived(sectionIssues.filter((issue) => issue.id === 'ticket_contract'));
  const timelineIssues = $derived(sectionIssues.filter((issue) => issue.id === 'timeline'));
  const artifactIssues = $derived(sectionIssues.filter((issue) => issue.id === 'artifacts'));
  const chatIssues = $derived(sectionIssues.filter((issue) => issue.id === 'linked_chat'));

  type TicketEditPayload = {
    title: string;
    agent: string;
    model: string;
    reasoning: string;
    done: boolean;
    body: string;
  };

  type TicketCreatePayload = {
    title: string;
    body: string;
  };

  let editTicketId = $state<string | null>(null);
  let editTitle = $state('');
  let editAgent = $state('');
  let editModel = $state('');
  let editReasoning = $state('');
  let editDone = $state(false);
  let editBody = $state('');
  let settingsSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let queueOpen = $state(false);
  let createOpen = $state(false);
  let queueMenuOpen = $state(false);
  let dragSourceRouteId = $state<string | null>(null);
  let dragTargetRouteId = $state<string | null>(null);
  let dragPlaceAfter = $state(false);
  let createTitle = $state('');
  let createBody = $state('');
  const ticketMarkdownContent = $derived(detail && editTicketId === detail.id ? editBody : detail?.rawBody ?? '');

  onDestroy(() => {
    if (settingsSaveTimer) clearTimeout(settingsSaveTimer);
  });

  function closeQueue(): void {
    queueOpen = false;
  }

  $effect(() => {
    if (!detail || editTicketId === detail.id) return;
    editTicketId = detail.id;
    editTitle = detail.title;
    editAgent = detail.agentLabel === 'Unassigned' ? 'codex' : detail.agentLabel;
    editModel = detail.modelLabel ?? '';
    editReasoning = detail.reasoningLabel ?? '';
    editDone = detail.done;
    editBody = detail.rawBody;
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
    return Boolean(await onSave({ title: editTitle, agent: editAgent, model: editModel, reasoning: editReasoning, done: editDone, body: editBody }));
  }

  async function saveMarkdown(_docId: string, content: string): Promise<boolean> {
    editBody = content;
    return Boolean(await onSave?.({ title: editTitle, agent: editAgent, model: editModel, reasoning: editReasoning, done: editDone, body: content }));
  }

  async function createTicket(): Promise<void> {
    if (!createTitle.trim()) return;
    const ok = await onCreateTicket?.({ title: createTitle, body: createBody });
    if (ok) {
      createTitle = '';
      createBody = '';
      createOpen = false;
    }
  }

  function routeNumber(routeId: string): number | null {
    const value = Number(routeId);
    return Number.isInteger(value) ? value : null;
  }

  function canDragTicketRow(row: TicketListRow): boolean {
    return Boolean(onReorderTicket && routeNumber(row.routeId) !== null);
  }

  function resetTicketDrag(): void {
    dragSourceRouteId = null;
    dragTargetRouteId = null;
    dragPlaceAfter = false;
  }

  function beginTicketDrag(event: DragEvent, row: TicketListRow): void {
    if (!canDragTicketRow(row)) {
      event.preventDefault();
      return;
    }
    dragSourceRouteId = row.routeId;
    dragTargetRouteId = row.routeId;
    dragPlaceAfter = false;
    event.dataTransfer?.setData('text/plain', row.routeId);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function updateTicketDropTarget(event: DragEvent, row: TicketListRow): void {
    if (!dragSourceRouteId || !canDragTicketRow(row)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    dragTargetRouteId = row.routeId;
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
    <PageHero title={list.title} subtitle={list.subtitle} />

    <div class="filter-row ticket-filter-row" role="tablist" aria-label="Ticket filters">
      {#each list.filters as filter}
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

    {#if actionStatus}
      <div class="state-panel">{actionStatus}</div>
    {/if}

    <section class="page-panel ticket-list-panel">
      <div class="panel-heading-row">
        <h2>{list.queueTitle}</h2>
        <div class="queue-heading-actions">
          {#if list.scopedOwner && onCreateTicket}
            <button type="button" class="ghost-button" onclick={() => (createOpen = !createOpen)} aria-expanded={createOpen}>
              {createOpen ? 'Cancel' : '+ New ticket'}
            </button>
          {/if}
          {#if list.scopedOwner}
            <div class="queue-menu-wrapper">
              <button
                type="button"
                class="ghost-button queue-menu-button"
                aria-haspopup="menu"
                aria-expanded={queueMenuOpen}
                aria-label="Ticket flow controls"
                onclick={() => (queueMenuOpen = !queueMenuOpen)}
              >
                <span aria-hidden="true">⋯</span>
              </button>
              {#if queueMenuOpen}
                <div class="queue-menu" role="menu">
                  <button type="button" role="menuitem" disabled={!queueActionEnabled('start')} title={queueActionReason('start')} onclick={() => { queueMenuOpen = false; onQueueCommand?.('start'); }}>{queueActionLabel('start')}</button>
                  <button type="button" role="menuitem" disabled={!queueActionEnabled('stop')} title={queueActionReason('stop')} onclick={() => { queueMenuOpen = false; onQueueCommand?.('stop'); }}>{queueActionLabel('stop')}</button>
                  <button type="button" role="menuitem" disabled={!queueActionEnabled('restart')} title={queueActionReason('restart')} onclick={() => { queueMenuOpen = false; onQueueCommand?.('restart'); }}>{queueActionLabel('restart')}</button>
                </div>
              {/if}
            </div>
          {/if}
        </div>
      </div>
      <p class={`ticket-flow-line ${list.flowStatus.signal}`} aria-label="Ticket flow status">
        <span class="status-pill {list.flowStatus.signal}">{list.flowStatus.statusLabel}</span>
        <span class="flow-line-meta">{list.flowStatus.progressLabel} done</span>
        {#if list.flowStatus.turnsLabel !== 'Unknown'}
          <span class="flow-line-meta">{list.flowStatus.turnsLabel} turns</span>
        {/if}
        {#if list.flowStatus.elapsedLabel !== 'Unknown'}
          <span class="flow-line-meta">{list.flowStatus.elapsedLabel}</span>
        {/if}
        {#if list.flowStatus.lastActivityLabel && list.flowStatus.lastActivityLabel !== 'No activity yet'}
          <span class="flow-line-meta">last activity {list.flowStatus.lastActivityLabel}</span>
        {/if}
        <span class="flow-line-divider" aria-hidden="true">·</span>
        <span class="flow-line-current">
          Current:
          {#if list.flowStatus.currentTicketHref}
            <a href={href(list.flowStatus.currentTicketHref)}>{list.flowStatus.currentTicketLabel}</a>
          {:else}
            {list.flowStatus.currentTicketLabel}
          {/if}
        </span>
        {#if list.flowStatus.reasonLabel && list.flowStatus.reasonLabel !== 'No reason reported'}
          <span class="flow-line-meta flow-line-reason">— {list.flowStatus.reasonLabel}</span>
        {/if}
      </p>
      {#if list.scopedOwner && onCreateTicket && createOpen}
        <form class="ticket-create-row" onsubmit={(event) => { event.preventDefault(); void createTicket(); }}>
          <input bind:value={createTitle} placeholder="New ticket title" aria-label="New ticket title" />
          <input bind:value={createBody} placeholder="Body preview or goal" aria-label="New ticket body" />
          <button type="submit" class="ghost-button" disabled={!createTitle.trim()}>Create ticket</button>
        </form>
      {/if}
      {@render degradedIssues(timelineIssues)}
      {@render degradedIssues(chatIssues)}
      {#if visibleRows.length === 0}
        <div class="state-panel empty-state compact-empty">
          <strong>No tickets in this view</strong>
          <p>Switch filters or ask PMA to create the next scoped ticket for the current CAR work.</p>
        </div>
      {:else}
        <div class="ticket-table" role="table" aria-label="Ticket queue">
          <div class="ticket-table-head" role="row">
            <span>Ticket</span>
            <span>Agent</span>
            <span>Status</span>
            <span>Run</span>
            <span>Updated</span>
            <span>Chat</span>
          </div>
          {#each visibleRows as row}
            <article
              class={`ticket-row ${row.status} ${dragSourceRouteId === row.routeId ? 'drag-source' : ''} ${dragTargetRouteId === row.routeId && !dragPlaceAfter && dragSourceRouteId !== row.routeId ? 'drop-before' : ''} ${dragTargetRouteId === row.routeId && dragPlaceAfter && dragSourceRouteId !== row.routeId ? 'drop-after' : ''}`}
              class:current={row.isCurrent}
              class:done={row.status === 'done'}
              ondragover={(event) => updateTicketDropTarget(event, row)}
              ondrop={(event) => void dropTicketRow(event, row)}
            >
              <span class="ticket-row-title">
                <button
                  type="button"
                  class="ticket-drag-handle"
                  draggable={canDragTicketRow(row)}
                  disabled={!canDragTicketRow(row)}
                  aria-label={`Drag ${row.numberLabel} to reorder`}
                  title={canDragTicketRow(row) ? 'Drag to reorder' : 'Only numbered scoped tickets can be reordered'}
                  ondragstart={(event) => beginTicketDrag(event, row)}
                  ondragend={resetTicketDrag}
                >
                  <span aria-hidden="true">☰</span>
                </button>
                <a class="ticket-row-title-link" href={href(row.href)} data-sveltekit-preload-data="tap">
                  <strong>{row.numberLabel}</strong>
                  <span>
                    {row.title}
                    {#if row.isCurrent}<em class="working-badge">Working</em>{/if}
                    {#if row.workspaceKind === 'unscoped'}<em class="working-badge needs-repair" title="Needs owner repair">Needs owner repair</em>{/if}
                    {#if row.bodyPreview}<small>{row.bodyPreview}</small>{/if}
                  </span>
                </a>
              </span>
              <span>{row.agentLabel}</span>
              <span>
                <span class="status-pill {row.status}">{statusLabel(row.status)}</span>
                {#if row.diffLabel}<small class="row-meta">{row.diffLabel}</small>{/if}
                {#if row.durationLabel}<small class="row-meta">{row.durationLabel}</small>{/if}
              </span>
              <span>
                {#if row.currentRunState}
                  <span class="status-pill {row.currentRunState}">{statusLabel(row.currentRunState)}</span>
                {:else}
                  <span class="muted">No run</span>
                {/if}
              </span>
              <span>{rowRelativeTime(row)}</span>
              <span>
                {#if row.chatHref}
                  <a class="inline-link" href={href(row.chatHref)}>PMA chat</a>
                {:else}
                  <span class="muted">None</span>
                {/if}
              </span>
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
    <header class="ticket-hero">
      <div class="ticket-hero-top">
        <nav class="ticket-breadcrumb" aria-label="Ticket breadcrumb">
          {#if detail.workspaceHref}
            <a class="ticket-breadcrumb-link" href={href(detail.workspaceHref)}>{detail.repoLabel}</a>
          {:else}
            <span class="ticket-breadcrumb-link">{detail.repoLabel}</span>
          {/if}
        </nav>
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
            <a class="ghost-button" href={href(detail.previousTicketHref)} aria-label="Previous ticket">‹ Prev</a>
          {/if}
          {#if detail.nextTicketHref}
            <a class="ghost-button" href={href(detail.nextTicketHref)} aria-label="Next ticket">Next ›</a>
          {/if}
        </div>
      </div>

      <h1 class="ticket-hero-title">{detail.title}</h1>

      <div class="ticket-hero-meta" aria-label="Ticket metadata">
        <span class="status-pill {detail.status}">{statusLabel(detail.status)}</span>
        <span class="meta-chip" title="Ticket number"><span class="meta-chip-key">#</span>{detail.numberLabel.replace(/^#/, '')}</span>
        <span class="meta-chip" title="Agent"><span class="meta-chip-key">Agent</span>{detail.agentLabel}</span>
        {#if detail.modelLabel}
          <span class="meta-chip" title="Model"><span class="meta-chip-key">Model</span>{detail.modelLabel}</span>
        {/if}
        {#if detail.reasoningLabel}
          <span class="meta-chip" title="Reasoning"><span class="meta-chip-key">Reasoning</span>{detail.reasoningLabel}</span>
        {/if}
        <span class="meta-chip" title="Updated"><span class="meta-chip-key">Updated</span>{detail.updatedLabel}</span>
        <span class="meta-chip" title="Progress"><span class="meta-chip-key">Progress</span>{detail.progressPercent}%</span>
        {#if detail.pathLabel}
          <span class="meta-chip meta-chip-path" title={detail.pathLabel}><span class="meta-chip-key">Path</span>{detail.pathLabel}</span>
        {/if}
      </div>

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

    {#if actionStatus}
      <div class="state-panel">{actionStatus}</div>
    {/if}
    {#if saveStatus}
      <div class="state-panel">{saveStatus}</div>
    {/if}

    {@render degradedIssues(contractIssues)}

    <div class="ticket-detail-layout">
      <main class="ticket-main-column">
        <details class="ticket-settings-disclosure">
          <summary>
            <span>Ticket settings</span>
            <span class="muted">Title, agent, model, reasoning, done</span>
          </summary>
          <div class="ticket-edit-grid">
            <label>
              <span>Title</span>
              <input bind:value={editTitle} oninput={() => scheduleSettingsSave()} />
            </label>
            <label>
              <span>Agent</span>
              <select bind:value={editAgent} onchange={() => scheduleSettingsSave(0)}>
                {#if editAgent && !['codex', 'claude', 'cursor'].includes(editAgent)}
                  <option value={editAgent}>{editAgent}</option>
                {/if}
                <option value="codex">codex</option>
                <option value="claude">claude</option>
                <option value="cursor">cursor</option>
              </select>
            </label>
            <label>
              <span>Model</span>
              <input bind:value={editModel} placeholder="default" oninput={() => scheduleSettingsSave()} />
            </label>
            <label>
              <span>Reasoning</span>
              <select bind:value={editReasoning} onchange={() => scheduleSettingsSave(0)}>
                <option value="">default</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="xhigh">xhigh</option>
              </select>
            </label>
            <label class="checkbox-row">
              <input type="checkbox" bind:checked={editDone} onchange={() => scheduleSettingsSave(0)} />
              <span>Done</span>
            </label>
          </div>
          {#if detail.workspacePathLabel}
            <p class="ticket-settings-foot muted">Workspace path: <code>{detail.workspacePathLabel}</code></p>
          {/if}
        </details>

        <section class="page-panel ticket-chat-panel">
          <div class="panel-heading-row">
            <h2>Ticket activity</h2>
            {#if detail.chatHref}
              <a class="inline-link" href={href(detail.chatHref)}>Open PMA chat</a>
            {/if}
          </div>
          {@render degradedIssues(chatIssues)}
          {#if detail.chatTranscriptCards.length === 0}
            <div class="state-panel empty-state compact-empty">
              <strong>No ticket activity yet</strong>
              <p>Streaming output and ticket-linked chat history appear here once PMA starts working this ticket.</p>
            </div>
          {:else}
            <details class="ticket-chat-history" open={ticketTranscriptStartsOpen(detail.status)}>
              <summary>
                <span>Chat history</span>
                <span class="muted">{detail.chatTranscriptCards.length} item{detail.chatTranscriptCards.length === 1 ? '' : 's'}</span>
              </summary>
              <div class="ticket-transcript-list">
                <PmaTranscriptCards cards={detail.chatTranscriptCards} />
              </div>
            </details>
          {/if}
        </section>

        <article class="ticket-markdown-card">
          <EditableMarkdown
            docId={detail.id}
            content={ticketMarkdownContent}
            html={renderMarkdownToHtml(ticketMarkdownContent)}
            isMissing={!ticketMarkdownContent.trim()}
            emptyTitle="No ticket markdown"
            emptyMessage="Add the ticket goal, tasks, acceptance criteria, or notes."
            editable={Boolean(onSave)}
            onSave={saveMarkdown}
          />
        </article>
      </main>

      <aside class="ticket-sidebar">
        <section class="page-panel execution-panel">
          <h2>Progress</h2>
          <span class="progress-track" aria-label={`${detail.progressPercent} percent complete`}>
            <span style={`width: ${detail.progressPercent}%`}></span>
          </span>
          <p>{detail.progressPercent}% · {statusLabel(detail.status)}</p>
        </section>

        <section class="page-panel execution-panel">
          <h2>Execution timeline</h2>
          {@render degradedIssues(timelineIssues)}
          {#if detail.timeline.length === 0}
            <div class="state-panel empty-state compact-empty">
              <strong>No run timeline</strong>
              <p>Resume or bootstrap this ticket to record execution events.</p>
            </div>
          {:else}
            <div class="timeline-list">
              {#each detail.timeline as item}
                <a class={`timeline-item ${item.status}`} href={href(item.href ?? detail.runHref ?? '/chats')}>
                  <span class="status-pill {item.status}">{statusLabel(item.status)}</span>
                  <span>
                    <strong>{item.title}</strong>
                    <span>{item.summary} · {rowRelativeTime({ updatedAt: item.timestamp })}</span>
                  </span>
                </a>
              {/each}
            </div>
          {/if}
        </section>

        <section class="page-panel execution-panel">
          <h2>Surfaced artifacts</h2>
          {@render degradedIssues(artifactIssues)}
          {#if detail.artifacts.length === 0}
            <div class="state-panel empty-state compact-empty">
              <strong>No artifacts surfaced</strong>
              <p>Screenshots, previews, files, and test summaries will appear after PMA work produces them.</p>
            </div>
          {:else}
            <div class="compact-activity-list">
              {#each detail.artifacts as artifact}
                <a class="dashboard-row activity-row" href={href(artifact.href ?? detail.runHref ?? '/chats')}>
                  <span class={`activity-kind ${artifact.kind}`}>{artifact.kind}</span>
                  <span>
                    <span class="row-title">{artifact.title}</span>
                    <span class="row-meta">{artifact.summary} · {rowRelativeTime({ createdAt: artifact.createdAt })}</span>
                  </span>
                </a>
              {/each}
            </div>
          {/if}
        </section>

        <section class="page-panel execution-panel">
          <h2>Linked chat</h2>
          {@render degradedIssues(chatIssues)}
          {#if detail.chatHref}
            <a class="dashboard-row compact-row" href={href(detail.chatHref)}>
              <span>
                <span class="row-title">PMA chat</span>
                <span class="row-meta">Ticket-linked conversation</span>
              </span>
              <span>Open</span>
            </a>
          {:else}
            <div class="state-panel empty-state compact-empty">
              <strong>No linked PMA chat</strong>
              <p>A ticket-linked conversation appears here after PMA starts discussing this ticket.</p>
            </div>
          {/if}
        </section>
      </aside>
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
                {#if row.diffLabel}<em class="diff-label">{row.diffLabel}</em>{/if}
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
