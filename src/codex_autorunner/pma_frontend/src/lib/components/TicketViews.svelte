<script lang="ts">
  import type {
    TicketDetailViewModel,
    TicketFilter,
    TicketListViewModel
  } from '$lib/viewModels/ticket';
  import { filterTicketRows, rowRelativeTime } from '$lib/viewModels/ticket';
  import { statusLabel } from '$lib/viewModels/pmaChat';

  let {
    state,
    mode,
    list = null,
    detail = null,
    selectedFilter = 'needs_attention',
    errorMessage = null,
    actionStatus = null,
    onFilter = undefined,
    onCommand = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'list' | 'detail';
    list?: TicketListViewModel | null;
    detail?: TicketDetailViewModel | null;
    selectedFilter?: TicketFilter;
    errorMessage?: string | null;
    actionStatus?: string | null;
    onFilter?: ((filter: TicketFilter) => void) | undefined;
    onCommand?: ((command: 'resume' | 'bootstrap') => void) | undefined;
  } = $props();

  const visibleRows = $derived(list ? filterTicketRows(list.rows, selectedFilter) : []);
</script>

{#if state === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading tickets...</div>
  </section>
{:else if state === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load tickets. {errorMessage}</div>
  </section>
{:else if mode === 'list' && list}
  <section class="page-stack ticket-page">
    <div class="section-heading">
      <p class="eyebrow">{list.eyebrow}</p>
      <h1>{list.title}</h1>
    </div>

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
        <h2>Current ticket queue</h2>
        <a href="/pma">Open PMA</a>
      </div>
      {#if visibleRows.length === 0}
        <div class="state-panel">No tickets match this filter.</div>
      {:else}
        <div class="ticket-table" role="table" aria-label="Ticket queue">
          <div class="ticket-table-head" role="row">
            <span>Ticket</span>
            <span>Repo/worktree</span>
            <span>Agent</span>
            <span>Status</span>
            <span>Run</span>
            <span>Updated</span>
            <span>Chat</span>
          </div>
          {#each visibleRows as row}
            <article class={`ticket-row ${row.status}`}>
              <a class="ticket-row-title" href={row.href}>
                <strong>{row.numberLabel}</strong>
                <span>{row.title}</span>
              </a>
              <span>{row.repoLabel}</span>
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
                  <a class="inline-link" href={row.chatHref}>PMA chat</a>
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
        <p class="eyebrow">Ticket detail</p>
        <h1>{detail.title}</h1>
        <p>{detail.numberLabel} · {detail.repoLabel} · {statusLabel(detail.status)} · {detail.updatedLabel}</p>
      </div>
      <div class="detail-actions">
        {#each detail.actions.filter((action) => !action.secondary) as action}
          {#if action.command}
            {@const command = action.command}
            <button type="button" onclick={() => onCommand?.(command)}>{action.label}</button>
          {:else if action.href}
            <a href={action.href}>{action.label}</a>
          {/if}
        {/each}
      </div>
    </div>

    {#if actionStatus}
      <div class="state-panel">{actionStatus}</div>
    {/if}

    <div class="ticket-detail-grid">
      <section class="page-panel ticket-contract-panel">
        <div class="panel-heading-row">
          <h2>Ticket contract</h2>
          <span class="status-pill {detail.status}">{statusLabel(detail.status)}</span>
        </div>
        <dl class="compact-definition ticket-definition">
          <div><dt>Number</dt><dd>{detail.numberLabel}</dd></div>
          <div><dt>Agent</dt><dd>{detail.agentLabel}</dd></div>
          <div><dt>Workspace</dt><dd>{detail.repoLabel}</dd></div>
        </dl>
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
          {#if detail.timeline.length === 0}
            <p>No execution timeline is available yet.</p>
          {:else}
            <div class="timeline-list">
              {#each detail.timeline as item}
                <a class={`timeline-item ${item.status}`} href={item.href ?? detail.runHref ?? '/tickets'}>
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
          {#if detail.artifacts.length === 0}
            <p>No surfaced artifacts are available yet.</p>
          {:else}
            <div class="compact-activity-list">
              {#each detail.artifacts as artifact}
                <a class="dashboard-row activity-row" href={artifact.href ?? detail.runHref ?? '/tickets'}>
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
          {#if detail.chatHref}
            <a class="dashboard-row compact-row" href={detail.chatHref}>
              <span>
                <span class="row-title">PMA chat</span>
                <span class="row-meta">Ticket-linked conversation</span>
              </span>
              <span>Open</span>
            </a>
          {:else}
            <p>No linked PMA chat is visible for this ticket.</p>
          {/if}
        </section>
      </aside>
    </div>

    <div class="secondary-actions">
      {#each detail.actions.filter((action) => action.secondary) as action}
        {#if action.href}<a href={action.href}>{action.label}</a>{/if}
      {/each}
    </div>
  </section>
{/if}
