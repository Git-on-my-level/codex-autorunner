<script lang="ts">
  import type {
    TicketDetailViewModel,
    TicketFilter,
    TicketListViewModel
  } from '$lib/viewModels/ticket';
  import { filterTicketRows, rowRelativeTime } from '$lib/viewModels/ticket';
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
    onSave?: ((payload: TicketEditPayload) => void | Promise<void>) | undefined;
  } = $props();

  const visibleRows = $derived(list ? filterTicketRows(list.rows, selectedFilter, selectedWorkspaceFilter) : []);
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

  function submitEdit(): void {
    void onSave?.({ title: editTitle, agent: editAgent, model: editModel, reasoning: editReasoning, done: editDone, body: editBody });
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

    <section class="page-panel ticket-list-panel">
      <div class="panel-heading-row">
        <h2>{list.queueTitle}</h2>
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
  <section class="page-stack ticket-page">
    <div class="section-heading detail-heading">
      <div>
        <p class="eyebrow">{detail.workspaceKind === 'repo' ? 'Repo ticket detail' : detail.workspaceKind === 'worktree' ? 'Worktree ticket detail' : 'Ticket detail'}</p>
        <h1>{detail.title}</h1>
        <p>{detail.numberLabel} · {detail.repoLabel} · {detail.pathLabel ?? 'ticket path unknown'} · {statusLabel(detail.status)} · {detail.updatedLabel}</p>
      </div>
      <div class="detail-actions">
        {#if detail.previousTicketHref}
          <a class="secondary-link" href={href(detail.previousTicketHref)}>Previous ticket</a>
        {/if}
        {#if detail.nextTicketHref}
          <a class="secondary-link" href={href(detail.nextTicketHref)}>Next ticket</a>
        {/if}
        {#if detail.ownerTicketListHref}
          <a class="secondary-link" href={href(detail.ownerTicketListHref)}>Back to {detail.workspaceKind} tickets</a>
        {/if}
        {#each detail.actions.filter((action) => !action.secondary) as action}
          {#if action.command}
            {@const command = action.command}
            <button type="button" onclick={() => onCommand?.(command)}>{action.label}</button>
          {:else if action.href}
            <a href={href(action.href)}>{action.label}</a>
          {/if}
        {/each}
      </div>
    </div>

    {#if actionStatus}
      <div class="state-panel">{actionStatus}</div>
    {/if}
    {#if saveStatus}
      <div class="state-panel">{saveStatus}</div>
    {/if}

    <div class="ticket-workspace-grid">
      <aside class="page-panel ticket-navigator-panel">
        <div class="panel-heading-row">
          <h2>{detail.workspaceKind === 'repo' ? 'Repo tickets' : detail.workspaceKind === 'worktree' ? 'Worktree tickets' : 'Tickets'}</h2>
          {#if detail.ownerTicketListHref}<a href={href(detail.ownerTicketListHref)}>Queue</a>{/if}
        </div>
        <div class="ticket-nav-list">
          {#each detail.sourceTickets as row}
            <a
              class={`ticket-nav-row ${row.status}`}
              class:active={row.routeId === detail.routeId || row.id === detail.id}
              href={href(row.href)}
              data-sveltekit-preload-data="tap"
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
      </aside>

      <div class="ticket-detail-grid">
      <section class="page-panel ticket-contract-panel">
        <div class="panel-heading-row">
          <h2>Ticket contract</h2>
          <span class="status-pill {detail.status}">{statusLabel(detail.status)}</span>
        </div>
        {@render degradedIssues(contractIssues)}
        <dl class="compact-definition ticket-definition">
          <div><dt>Number</dt><dd>{detail.numberLabel}</dd></div>
          <div><dt>Agent</dt><dd>{detail.agentLabel}</dd></div>
          {#if detail.modelLabel}<div><dt>Model</dt><dd>{detail.modelLabel}</dd></div>{/if}
          {#if detail.reasoningLabel}<div><dt>Reasoning</dt><dd>{detail.reasoningLabel}</dd></div>{/if}
          <div>
            <dt>Owner</dt>
            <dd>
              {#if detail.workspaceHref}
                <a href={href(detail.workspaceHref)}>{detail.repoLabel}</a>
              {:else}
                {detail.repoLabel}
              {/if}
            </dd>
          </div>
          {#if detail.pathLabel}
            <div><dt>Ticket path</dt><dd>{detail.pathLabel}</dd></div>
          {/if}
          {#if detail.workspacePathLabel}
            <div><dt>Workspace path</dt><dd>{detail.workspacePathLabel}</dd></div>
          {/if}
        </dl>
        <section class="ticket-editor-panel">
          <div class="panel-heading-row">
            <h3>Edit ticket</h3>
            <button type="button" onclick={submitEdit} disabled={!onSave}>Save</button>
          </div>
          <div class="ticket-edit-grid">
            <label>
              <span>Title</span>
              <input bind:value={editTitle} />
            </label>
            <label>
              <span>Agent</span>
              <select bind:value={editAgent}>
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
              <input bind:value={editModel} placeholder="default" />
            </label>
            <label>
              <span>Reasoning</span>
              <select bind:value={editReasoning}>
                <option value="">default</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="xhigh">xhigh</option>
              </select>
            </label>
            <label class="checkbox-row">
              <input type="checkbox" bind:checked={editDone} />
              <span>Done</span>
            </label>
          </div>
          <textarea bind:value={editBody} rows="16" spellcheck="false"></textarea>
        </section>
        {#if detail.goal}
          <section class="contract-section">
            <h3>Goal</h3>
            <p>{detail.goal}</p>
          </section>
        {/if}
        {#each detail.contractSections as section}
          <section class="contract-section">
            <h3>{section.title}</h3>
            {#if section.body}<p>{section.body}</p>{/if}
            {#if section.items.length}
              <ul>
                {#each section.items as item}
                  <li>{item}</li>
                {/each}
              </ul>
            {/if}
          </section>
        {/each}
      </section>

      <aside class="ticket-side">
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
    </div>

    <div class="secondary-actions">
      {#each detail.actions.filter((action) => action.secondary) as action}
        {#if action.href}<a href={href(action.href)}>{action.label}</a>{/if}
      {/each}
    </div>
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
