<script lang="ts">
  import { onDestroy } from 'svelte';
  import type {
    TicketDetailViewModel,
    TicketFilter,
    TicketListViewModel
  } from '$lib/viewModels/ticket';
  import EditableMarkdown from '$lib/components/EditableMarkdown.svelte';
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
    onSave?: ((payload: TicketEditPayload) => boolean | Promise<boolean>) | undefined;
  } = $props();

  const visibleRows = $derived(list ? filterTicketRows(list.rows, selectedFilter, selectedWorkspaceFilter) : []);
  const queueBusy = $derived(Boolean(list?.queueRun?.status === 'running'));
  const canStopQueue = $derived(Boolean(list?.queueRun?.id && ['running', 'waiting', 'blocked', 'failed'].includes(list.queueRun.status)));
  const canRestartQueue = $derived(Boolean(list?.queueRun?.id && visibleRows.length > 0));
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

  let editTicketId = $state<string | null>(null);
  let editTitle = $state('');
  let editAgent = $state('');
  let editModel = $state('');
  let editReasoning = $state('');
  let editDone = $state(false);
  let editBody = $state('');
  let settingsSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let queueOpen = $state(false);
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
    <div class="section-heading">
      <p class="eyebrow">{list.eyebrow}</p>
      <h1>{list.title}</h1>
      <p>{list.subtitle}</p>
    </div>

    {#if !list.scopedOwner}
      <div class="filter-row ticket-filter-row" role="tablist" aria-label="Workspace filters">
        {#each list.workspaceFilters as filter}
          <a
            class:active={selectedWorkspaceFilter === filter.id}
            class="chip"
            role="tab"
            aria-selected={selectedWorkspaceFilter === filter.id}
            href={href(filter.id === 'all'
              ? '/tickets'
              : filter.id === 'unscoped'
                ? '/tickets?unscoped=1'
                : `/tickets?${filter.id.startsWith('repo:') ? 'repo' : 'worktree'}=${encodeURIComponent(filter.id.split(':')[1] ?? '')}`)}
          >
            {filter.label}
            <span>{filter.count}</span>
          </a>
        {/each}
      </div>
    {/if}

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
        {#if list.scopedOwner}
          <div class="queue-actions" aria-label="Ticket flow controls">
            <button type="button" class="ghost-button" disabled={queueBusy} onclick={() => onQueueCommand?.('start')}>
              {queueBusy ? 'Running' : 'Start'}
            </button>
            <button type="button" class="ghost-button" disabled={!canStopQueue} onclick={() => onQueueCommand?.('stop')}>Stop</button>
            <button type="button" class="ghost-button" disabled={!canRestartQueue} onclick={() => onQueueCommand?.('restart')}>Restart</button>
          </div>
        {/if}
      </div>
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
            <span>Workspace scope</span>
            <span>Agent</span>
            <span>Status</span>
            <span>Run</span>
            <span>Updated</span>
            <span>Chat</span>
          </div>
          {#each visibleRows as row}
            <article class={`ticket-row ${row.status}`}>
              <a class="ticket-row-title" href={href(row.href)} data-sveltekit-preload-data="tap">
                <strong>{row.numberLabel}</strong>
                <span>{row.title}</span>
              </a>
              <span>
                {#if row.workspaceHref}
                  <a class="inline-link" href={href(row.workspaceHref)}>{row.repoLabel}</a>
                {:else}
                  {row.repoLabel}
                {/if}
                {#if row.pathLabel}<small class="row-meta">{row.pathLabel}</small>{/if}
              </span>
              <span>{row.agentLabel}</span>
              <span><span class="status-pill {row.status}">{statusLabel(row.status)}</span></span>
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
          <p class="eyebrow">{detail.workspaceKind === 'repo' ? 'Repo ticket' : detail.workspaceKind === 'worktree' ? 'Worktree ticket' : 'Ticket'}</p>
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
                <a class={`timeline-item ${item.status}`} href={href(item.href ?? detail.runHref ?? '/tickets')}>
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
                <a class="dashboard-row activity-row" href={href(artifact.href ?? detail.runHref ?? '/tickets')}>
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
