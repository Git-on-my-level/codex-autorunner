<script lang="ts">
  import type {
    RepoWorktreeDetailViewModel,
    RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';
  import { rowRelativeTime } from '$lib/viewModels/repoWorktree';
  import { statusLabel } from '$lib/viewModels/pmaChat';

  let {
    state,
    mode,
    index = null,
    detail = null,
    errorMessage = null
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'index' | 'detail';
    index?: RepoWorktreeIndexViewModel | null;
    detail?: RepoWorktreeDetailViewModel | null;
    errorMessage?: string | null;
  } = $props();
</script>

{#if state === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading workspace state...</div>
  </section>
{:else if state === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load workspace state. {errorMessage}</div>
  </section>
{:else if mode === 'index' && index}
  <section class="page-stack repo-worktree-page">
    <div class="section-heading">
      <p class="eyebrow">{index.eyebrow}</p>
      <h1>{index.title}</h1>
    </div>

    <div class="summary-grid compact-summary">
      <div class="metric-card neutral">
        <span>Active</span>
        <strong>{index.activeCount}</strong>
      </div>
      <div class="metric-card waiting">
        <span>Waiting</span>
        <strong>{index.waitingCount}</strong>
      </div>
      <div class="metric-card neutral">
        <span>Open tickets</span>
        <strong>{index.openTicketCount}</strong>
      </div>
    </div>

    <section class="page-panel workspace-index-panel">
      <div class="panel-heading-row">
        <h2>Current work</h2>
        <a href="/pma">Open PMA</a>
      </div>
      {#if index.rows.length === 0}
        <p>No repos or worktrees are registered yet.</p>
      {:else}
        <div class="workspace-row-list">
          {#each index.rows as row}
            <article class={`workspace-row ${row.status}`} class:has-children={row.childWorktrees.length > 0}>
              <div class="workspace-row-shell">
                <a class="workspace-row-main" href={row.href}>
                  <span class="status-pill {row.status}">{statusLabel(row.status)}</span>
                  <span>
                    <strong>{row.label}</strong>
                    <span>{row.kind === 'worktree' ? 'repo child' : 'repo'} · {row.detail} · {rowRelativeTime(row)}</span>
                  </span>
                </a>
                <div class="workspace-row-meta">
                  <span>{row.activeRuns} runs</span>
                  <span>{row.openTickets} tickets</span>
                  {#if row.repoHref}<a href={row.repoHref}>Parent repo</a>{/if}
                </div>
              </div>
              {#if row.childWorktrees.length > 0}
                <div class="child-worktree-list" aria-label={`Worktrees owned by ${row.label}`}>
                  {#each row.childWorktrees as worktree}
                    <a class={`child-worktree-row ${worktree.status}`} href={worktree.href}>
                      <span class="status-pill {worktree.status}">{statusLabel(worktree.status)}</span>
                      <span>
                        <strong>{worktree.label}</strong>
                        <span>
                          branch {worktree.branch ?? 'unknown'} · {worktree.openTickets} open ticket{worktree.openTickets === 1 ? '' : 's'}{#if worktree.currentRunTitle} · {worktree.currentRunTitle}{/if}
                        </span>
                      </span>
                    </a>
                  {/each}
                </div>
              {/if}
            </article>
          {/each}
        </div>
      {/if}
    </section>
  </section>
{:else if mode === 'detail' && detail}
  <section class="page-stack repo-worktree-page">
    <div class="section-heading detail-heading">
      <div>
        <p class="eyebrow">{detail.eyebrow}</p>
        <h1>{detail.title}</h1>
        <p>{detail.kind === 'worktree' ? 'repo worktree variant' : 'repo'} · {detail.stateLabel}{#if detail.branch} · {detail.branch}{/if}</p>
      </div>
      <div class="detail-actions">
        {#each detail.links.filter((link) => !link.secondary) as link}
          <a href={link.href}>{link.label}</a>
        {/each}
      </div>
    </div>

    <section class="page-panel identity-panel">
      <h2>{detail.kind === 'worktree' ? 'Repo worktree' : 'Repo'} identity</h2>
      <dl class="compact-definition">
        <div><dt>ID</dt><dd>{detail.id}</dd></div>
        {#if detail.kind === 'worktree'}
          <div>
            <dt>Base repo</dt>
            <dd>
              {#if detail.baseRepoHref}<a href={detail.baseRepoHref}>{detail.baseRepoLabel}</a>{:else}{detail.baseRepoLabel ?? 'Unknown'}{/if}
            </dd>
          </div>
        {/if}
        <div><dt>Branch</dt><dd>{detail.branch ?? 'Unknown'}</dd></div>
        <div><dt>Path</dt><dd>{detail.path ?? 'Unknown'}</dd></div>
      </dl>
    </section>

    <div class="detail-grid">
      <section class="page-panel execution-panel wide">
        <div class="panel-heading-row">
          <h2>Current run</h2>
          <a href="/tickets">Workspace tickets</a>
        </div>
        {#if detail.currentRuns.length === 0 || !detail.hasActiveRun}
          <div class="state-panel">No active ticket run is visible for this {detail.kind === 'worktree' ? 'worktree' : 'repo'}.</div>
        {/if}
        {#each detail.currentRuns as run}
          <article class={`run-card ${run.status}`}>
            <div class="run-card-main">
              <span class="status-pill {run.status}">{statusLabel(run.status)}</span>
              <div>
                <h3>{run.title}</h3>
                <p>
                  {#if run.agentId}{run.agentId} · {/if}{run.phase ?? 'run activity'} · {rowRelativeTime({ updatedAt: run.updatedAt })}
                </p>
              </div>
            </div>
            <span class="progress-track" aria-label={`${run.progress} percent complete`}>
              <span style={`width: ${run.progress}%`}></span>
            </span>
            <div class="row-links">
              {#if run.chatHref}<a href={run.chatHref}>PMA chat</a>{/if}
              {#if run.ticketHref}<a href={run.ticketHref}>Ticket</a>{/if}
              {#if run.logsHref}<a class="secondary-link" href={run.logsHref}>Debug logs</a>{/if}
            </div>
          </article>
        {/each}
      </section>

      <section class="page-panel execution-panel">
        <h2>Activity</h2>
        {@render compactList(detail.activity, 'No live activity summary is available yet.')}
      </section>

      {#if detail.kind === 'repo'}
        <section class="page-panel execution-panel wide">
          <h2>Child worktrees</h2>
          {#if detail.childWorktrees.length === 0}
            <p>No worktrees are registered for this repo.</p>
          {:else}
            <div class="child-worktree-list detail-child-worktrees">
              {#each detail.childWorktrees as worktree}
                <article class={`child-worktree-row ${worktree.status}`}>
                  <a href={worktree.href}>
                    <span class="status-pill {worktree.status}">{statusLabel(worktree.status)}</span>
                    <span>
                      <strong>{worktree.label}</strong>
                      <span>
                        branch {worktree.branch ?? 'unknown'} · {worktree.openTickets} open ticket{worktree.openTickets === 1 ? '' : 's'}{#if worktree.currentRunTitle} · {worktree.currentRunTitle}{/if}
                      </span>
                    </span>
                  </a>
                  <div class="workspace-row-meta">
                    <span>{worktree.activeRuns} runs</span>
                    {#if worktree.ticketHref}<a href={worktree.ticketHref}>Current ticket</a>{/if}
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/if}

      <section class="page-panel execution-panel">
        <h2>Workspace tickets</h2>
        {#if detail.currentTickets.length === 0}
          <p>No current ticket is associated with the visible run.</p>
        {:else}
          <div class="compact-link-list">
            {#each detail.currentTickets as ticket}
              <a href={ticket.href}>{ticket.title}<span>{statusLabel(ticket.status)}</span></a>
            {/each}
          </div>
        {/if}
      </section>

      <section class="page-panel execution-panel">
        <h2>Next workspace tickets</h2>
        {#if detail.nextTickets.length === 0}
          <p>No next tickets are queued for this {detail.kind === 'worktree' ? 'worktree' : 'repo'}.</p>
        {:else}
          <div class="compact-link-list">
            {#each detail.nextTickets as ticket}
              <a href={ticket.href}>{ticket.title}<span>{statusLabel(ticket.status)}</span></a>
            {/each}
          </div>
        {/if}
      </section>

      <section class="page-panel execution-panel wide">
        <h2>Surfaced artifacts</h2>
        {@render compactList(detail.artifacts, 'No surfaced artifacts are available yet.')}
      </section>
    </div>

    <div class="secondary-actions">
      {#each detail.links.filter((link) => link.secondary) as link}
        <a href={link.href}>{link.label}</a>
      {/each}
    </div>
  </section>
{/if}

{#snippet compactList(items: { id: string; title: string; summary: string; href: string | null; kind: string; createdAt: string | null }[], emptyText: string)}
  {#if items.length === 0}
    <p>{emptyText}</p>
  {:else}
    <div class="activity-list compact-activity-list">
      {#each items as item}
        <a class="dashboard-row activity-row" href={item.href ?? '/pma'}>
          <span class={`activity-kind ${item.kind}`}>{item.kind}</span>
          <span>
            <span class="row-title">{item.title}</span>
            <span class="row-meta">{item.summary} · {rowRelativeTime({ createdAt: item.createdAt })}</span>
          </span>
        </a>
      {/each}
    </div>
  {/if}
{/snippet}
