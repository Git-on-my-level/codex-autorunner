<script lang="ts">
  import type {
    RepoWorktreeDetailViewModel,
    RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';
  import { rowRelativeTime } from '$lib/viewModels/repoWorktree';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { PartialPageIssue } from '$lib/api/client';

  let {
    state,
    mode,
    index = null,
    detail = null,
    errorMessage = null,
    sectionIssues = [],
    onRetry = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'index' | 'detail';
    index?: RepoWorktreeIndexViewModel | null;
    detail?: RepoWorktreeDetailViewModel | null;
    errorMessage?: string | null;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
  } = $props();

  const currentRunIssues = $derived(sectionIssues.filter((issue) => issue.id === 'current_run'));
  const ticketIssues = $derived(sectionIssues.filter((issue) => issue.id === 'tickets'));
  const artifactIssues = $derived(sectionIssues.filter((issue) => issue.id === 'artifacts'));
  const queueTickets = $derived(detail ? [...detail.currentTickets, ...detail.nextTickets] : []);

  function repoInitials(label: string): string {
    if (!label) return '·';
    const cleaned = label.replace(/[_\-./]+/g, ' ').trim();
    const words = cleaned.split(/\s+/).filter(Boolean);
    if (words.length === 0) return label.slice(0, 1).toUpperCase();
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[1][0]).toUpperCase();
  }

  // Deterministic accent color per repo based on label hash.
  function repoAccent(label: string): string {
    const palette = [
      '#5b5fc7', // indigo
      '#117a4d', // green
      '#9a5b00', // amber
      '#b42424', // red
      '#0f7285', // teal
      '#7a3fb8', // violet
      '#1f6fda', // blue
      '#c0497d'  // pink
    ];
    let hash = 0;
    for (let i = 0; i < label.length; i++) {
      hash = (hash * 31 + label.charCodeAt(i)) >>> 0;
    }
    return palette[hash % palette.length];
  }
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
  <section class="page-stack repo-worktree-page repos-index-v2">
    <header class="repos-hero">
      <div class="repos-hero-copy">
        <h1>{index.title}</h1>
        <p class="repos-hero-sub">Workspaces and worktrees connected to PMA.</p>
      </div>
      <dl class="repos-hero-stats" aria-label="Workspace summary">
        <div class={index.activeCount > 0 ? 'is-active' : ''}>
          <dt>Active</dt>
          <dd>{index.activeCount}</dd>
        </div>
        <div class={index.waitingCount > 0 ? 'is-waiting' : ''}>
          <dt>Waiting</dt>
          <dd>{index.waitingCount}</dd>
        </div>
        <div>
          <dt>Tickets</dt>
          <dd>{index.openTicketCount}</dd>
        </div>
      </dl>
    </header>

    {@render degradedIssues(currentRunIssues)}
    {@render degradedIssues(ticketIssues)}

    {#if index.rows.length === 0}
      <div class="state-panel empty-state compact-empty repos-empty">
        <strong>No repos registered</strong>
        <p>Register a workspace before queueing repo-scoped tickets.</p>
      </div>
    {:else}
      <ul class="repos-list" role="list">
        {#each index.rows as row}
          {@const accent = repoAccent(row.label)}
          <li class={`repo-item status-${row.status}`} class:has-children={row.childWorktrees.length > 0} style={`--repo-accent: ${accent};`}>
            <a class="repo-card" href={href(row.href)}>
              <span class="repo-avatar" aria-hidden="true">{repoInitials(row.label)}</span>
              <div class="repo-card-body">
                <div class="repo-card-title">
                  <span class="repo-name">{row.label}</span>
                  <span class={`repo-status status-pill ${row.status}`}>{statusLabel(row.status)}</span>
                </div>
                <div class="repo-card-meta">
                  <span class="repo-meta-kind">{row.kind === 'worktree' ? 'worktree' : 'repo'}</span>
                  {#if row.branch}
                    <span class="repo-meta-dot" aria-hidden="true">·</span>
                    <span class="repo-meta-branch"><span class="repo-meta-icon" aria-hidden="true">⎇</span>{row.branch}</span>
                  {/if}
                  {#if row.detail}
                    <span class="repo-meta-dot" aria-hidden="true">·</span>
                    <span>{row.detail}</span>
                  {/if}
                  <span class="repo-meta-dot" aria-hidden="true">·</span>
                  <span class="repo-meta-time">{rowRelativeTime(row)}</span>
                </div>
              </div>
              <div class="repo-card-counts" aria-label="Activity counts">
                <span class={`count-chip ${row.activeRuns > 0 ? 'is-active' : ''}`} title="Active runs">
                  <strong>{row.activeRuns}</strong><em>run{row.activeRuns === 1 ? '' : 's'}</em>
                </span>
                <span class={`count-chip ${row.openTickets > 0 ? 'is-tickets' : ''}`} title="Open tickets">
                  <strong>{row.openTickets}</strong><em>ticket{row.openTickets === 1 ? '' : 's'}</em>
                </span>
              </div>
              <span class="repo-chevron" aria-hidden="true">→</span>
            </a>

            {#if row.childWorktrees.length > 0}
              <ul class="worktree-list" role="list" aria-label={`Worktrees owned by ${row.label}`}>
                {#each row.childWorktrees as worktree}
                  <li class={`worktree-item status-${worktree.status}`}>
                    <div class="worktree-card">
                      <span class="worktree-rail" aria-hidden="true"></span>
                      <span class="worktree-dot" aria-hidden="true"></span>
                      <div class="worktree-card-body">
                        <div class="worktree-card-title">
                          <a class="worktree-name" href={href(worktree.href)}>{worktree.label}</a>
                          <span class={`status-pill ${worktree.status}`}>{statusLabel(worktree.status)}</span>
                        </div>
                        <div class="worktree-card-meta">
                          {#if worktree.branch}
                            <span class="repo-meta-branch"><span class="repo-meta-icon" aria-hidden="true">⎇</span>{worktree.branch}</span>
                            <span class="repo-meta-dot" aria-hidden="true">·</span>
                          {/if}
                          <span>{worktree.openTickets} open ticket{worktree.openTickets === 1 ? '' : 's'}</span>
                          {#if worktree.currentRunTitle}
                            <span class="repo-meta-dot" aria-hidden="true">·</span>
                            <span class="worktree-run-title">{worktree.currentRunTitle}</span>
                          {/if}
                        </div>
                      </div>
                      <div class="worktree-card-counts">
                        {#if worktree.activeRuns > 0}
                          <span class="count-chip is-active" title="Active runs">
                            <strong>{worktree.activeRuns}</strong><em>run{worktree.activeRuns === 1 ? '' : 's'}</em>
                          </span>
                        {/if}
                        <span class={`count-chip ${worktree.openTickets > 0 ? 'is-tickets' : ''}`} title="Open worktree tickets">
                          <strong>{worktree.openTickets}</strong><em>ticket{worktree.openTickets === 1 ? '' : 's'}</em>
                        </span>
                        {#if worktree.ticketHref}
                          <a class="queue-link" href={href(worktree.ticketHref)}>Tickets</a>
                        {/if}
                      </div>
                    </div>
                  </li>
                {/each}
              </ul>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{:else if mode === 'detail' && detail}
  <section class="page-stack repo-worktree-page">
    {#if detail.isMissing}
      <div class="section-heading detail-heading">
        <div>
          <p class="eyebrow">{detail.eyebrow}</p>
          <h1>{detail.title}</h1>
          <p>The route id <code>{detail.id}</code> does not match a known {detail.kind} in the current hub inventory.</p>
        </div>
        <div class="detail-actions">
          <a href={href(detail.missingIndexHref)}>{detail.missingIndexLabel}</a>
        </div>
      </div>

      <section class="page-panel identity-panel">
        <h2>Unknown workspace</h2>
        <div class="state-panel empty-state compact-empty">
          <strong>No matching {detail.kind}</strong>
          <p>Refresh the workspace inventory or choose a known {detail.kind} from the index before opening scoped tickets, runs, or contextspace.</p>
        </div>
      </section>
    {:else}
    <div class="section-heading detail-heading">
      <div>
        <p class="eyebrow">{detail.eyebrow}</p>
        <h1>{detail.title}</h1>
        <p>{detail.kind === 'worktree' ? 'repo worktree variant' : 'repo'} · {detail.stateLabel}{#if detail.branch} · {detail.branch}{/if}</p>
      </div>
      <div class="detail-actions">
        {#each detail.links.filter((link) => !link.secondary) as link}
          <a href={href(link.href)}>{link.label}</a>
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
              {#if detail.baseRepoHref}<a href={href(detail.baseRepoHref)}>{detail.baseRepoLabel}</a>{:else}{detail.baseRepoLabel ?? 'Unknown'}{/if}
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
          <a href={href(detail.ticketIndexHref)}>{detail.ticketIndexLabel}</a>
        </div>
        {@render degradedIssues(currentRunIssues)}
        {#if detail.currentRuns.length === 0 || !detail.hasActiveRun}
          <div class="state-panel empty-state compact-empty">
            <strong>No active run</strong>
            <p>Use PMA or the ticket queue to start the next {detail.kind === 'worktree' ? 'worktree' : 'repo'} ticket.</p>
          </div>
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
              {#if run.chatHref}<a href={href(run.chatHref)}>PMA chat</a>{/if}
              {#if run.ticketHref}<a href={href(run.ticketHref)}>Ticket</a>{/if}
              {#if run.logsHref}<a class="secondary-link" href={href(run.logsHref)}>Debug logs</a>{/if}
            </div>
          </article>
        {/each}
      </section>

      <section class="page-panel execution-panel wide workspace-ticket-queue-panel">
        <div class="panel-heading-row">
          <h2>{detail.kind === 'worktree' ? 'Worktree ticket queue' : 'Repo ticket queue'}</h2>
          <a href={href(detail.ticketIndexHref)}>{detail.ticketIndexLabel}</a>
        </div>
        {@render degradedIssues(ticketIssues)}
        {#if queueTickets.length === 0}
          <div class="state-panel empty-state compact-empty">
            <strong>No queued tickets</strong>
            <p>Add a ticket for this {detail.kind === 'worktree' ? 'worktree' : 'repo'} when there is follow-up work.</p>
          </div>
        {:else}
          <div class="workspace-ticket-list">
            {#each queueTickets as ticket}
              <a class={`workspace-ticket-row ${ticket.status}`} href={href(ticket.href)}>
                <span>
                  <strong>{ticket.title}</strong>
                  {#if ticket.diffLabel}<em>{ticket.diffLabel}</em>{/if}
                </span>
                <span class="status-pill {ticket.status}">{statusLabel(ticket.status)}</span>
              </a>
            {/each}
          </div>
        {/if}
      </section>

      <section class="page-panel execution-panel">
        <h2>Activity</h2>
        {@render compactList(detail.activity, 'No live activity summary is available yet.')}
      </section>

      {#if detail.kind === 'repo'}
        <section class="page-panel execution-panel wide">
        <h2>Child worktrees</h2>
        {#if detail.childWorktrees.length === 0}
            <div class="state-panel empty-state compact-empty">
              <strong>No child worktrees</strong>
              <p>Create a worktree when a ticket needs isolated repo state.</p>
            </div>
          {:else}
            <div class="child-worktree-list detail-child-worktrees">
              {#each detail.childWorktrees as worktree}
                <article class={`child-worktree-row ${worktree.status}`}>
                  <a href={href(worktree.href)}>
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
                    {#if worktree.ticketHref}<a href={href(worktree.ticketHref)}>Current ticket</a>{/if}
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/if}

      <section class="page-panel execution-panel wide">
        <h2>Surfaced artifacts</h2>
        {@render degradedIssues(artifactIssues)}
        {@render compactList(detail.artifacts, 'No surfaced artifacts are available yet.')}
      </section>
    </div>

    <div class="secondary-actions">
      {#each detail.links.filter((link) => link.secondary) as link}
        <a href={href(link.href)}>{link.label}</a>
      {/each}
    </div>
    {/if}
  </section>
{/if}

{#snippet compactList(items: { id: string; title: string; summary: string; href: string | null; kind: string; createdAt: string | null }[], emptyText: string)}
  {#if items.length === 0}
    <div class="state-panel empty-state compact-empty">
      <strong>No entries yet</strong>
      <p>{emptyText}</p>
    </div>
  {:else}
    <div class="activity-list compact-activity-list">
      {#each items as item}
        <a class="dashboard-row activity-row" href={href(item.href ?? '/pma')}>
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
  .repos-index-v2 {
    gap: var(--space-3);
  }

  .repos-hero {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-5);
    padding: 0 2px;
  }

  .repos-hero-copy {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .repos-hero h1 {
    margin: 0;
    font-size: var(--font-size-4);
    font-weight: 650;
    letter-spacing: -0.018em;
    line-height: 1.2;
  }

  .repos-hero-sub {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
    line-height: 1.4;
  }

  .repos-hero-stats {
    display: flex;
    align-items: stretch;
    gap: 0;
    margin: 0;
    padding: 4px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface);
  }

  .repos-hero-stats > div {
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 2px var(--space-3);
    border-right: 1px solid var(--color-border-subtle);
  }

  .repos-hero-stats > div:last-child {
    border-right: 0;
  }

  .repos-hero-stats dt {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0;
    text-transform: none;
  }

  .repos-hero-stats dd {
    margin: 0;
    color: var(--color-ink);
    font-size: var(--font-size-2);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
    line-height: 1;
  }

  .repos-hero-stats > div.is-active dd { color: var(--color-success); }
  .repos-hero-stats > div.is-waiting dd { color: var(--color-warning); }
  .repos-hero-stats > div.is-active dt { color: var(--color-success); }
  .repos-hero-stats > div.is-waiting dt { color: var(--color-warning); }

  .repos-empty {
    border-radius: 12px;
    padding: var(--space-5);
  }

  .repos-list {
    display: grid;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .repo-item {
    --repo-accent: var(--color-accent);
    position: relative;
    border: 1px solid var(--color-border-subtle);
    border-radius: 12px;
    background: var(--color-surface);
    box-shadow: 0 1px 0 rgb(15 15 20 / 0.02);
    overflow: hidden;
    transition: border-color var(--transition-base), box-shadow var(--transition-base), transform var(--transition-base);
  }

  .repo-item:hover {
    border-color: var(--color-border-strong);
    box-shadow: 0 8px 24px -16px rgb(15 15 20 / 0.18), 0 2px 6px -3px rgb(15 15 20 / 0.06);
  }

  .repo-card {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-4) var(--space-5);
    color: var(--color-ink);
    text-decoration: none;
  }

  .repo-avatar {
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: color-mix(in srgb, var(--repo-accent) 12%, white);
    color: var(--repo-accent);
    font-weight: 650;
    font-size: 13px;
    letter-spacing: -0.01em;
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--repo-accent) 18%, transparent);
  }

  .repo-card-body {
    min-width: 0;
    display: grid;
    gap: 4px;
  }

  .repo-card-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }

  .repo-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--color-ink);
    font-size: var(--font-size-2);
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .repo-card-meta,
  .worktree-card-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.4;
    min-width: 0;
  }

  .repo-meta-kind {
    display: inline-flex;
    align-items: center;
    height: 18px;
    padding: 0 6px;
    border-radius: 4px;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .repo-meta-dot {
    color: var(--color-ink-faint);
    opacity: 0.7;
  }

  .repo-meta-branch {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: var(--color-ink-soft);
  }

  .repo-meta-icon {
    color: var(--color-ink-faint);
    font-size: 12px;
    line-height: 1;
  }

  .repo-meta-time {
    color: var(--color-ink-faint);
    font-variant-numeric: tabular-nums;
  }

  .repo-card-counts,
  .worktree-card-counts {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .count-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
    min-height: 24px;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 11px;
    font-weight: 500;
    line-height: 1.2;
    white-space: nowrap;
    transition: background-color var(--transition-fast), color var(--transition-fast);
  }

  .count-chip strong {
    color: var(--color-ink);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .count-chip em {
    font-style: normal;
    color: var(--color-ink-muted);
  }

  .count-chip.is-active {
    background: var(--color-success-soft);
    color: var(--color-success);
  }

  .count-chip.is-active strong,
  .count-chip.is-active em {
    color: var(--color-success);
  }

  .count-chip.is-tickets {
    background: var(--color-accent-soft);
    color: var(--color-accent);
  }

  .count-chip.is-tickets strong,
  .count-chip.is-tickets em {
    color: var(--color-accent);
  }

  .repo-chevron {
    color: var(--color-ink-faint);
    font-size: 14px;
    line-height: 1;
    opacity: 0;
    transform: translateX(-4px);
    transition: opacity var(--transition-base), transform var(--transition-base), color var(--transition-base);
  }

  .repo-item:hover .repo-chevron {
    opacity: 1;
    transform: translateX(0);
    color: var(--color-ink);
  }

  .repo-status {
    text-transform: lowercase;
  }

  /* Worktree children — Linear-style nested list with rail */
  .worktree-list {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0 var(--space-5) var(--space-3);
    list-style: none;
    background: linear-gradient(180deg, transparent, var(--color-surface-sunken) 8%);
    border-top: 1px solid var(--color-border-subtle);
  }

  .repo-item.has-children .repo-card {
    padding-bottom: var(--space-3);
  }

  .worktree-item + .worktree-item {
    border-top: 1px solid var(--color-border-subtle);
  }

  .worktree-card {
    position: relative;
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) 0 var(--space-2) 8px;
    color: var(--color-ink);
    text-decoration: none;
    border-radius: 6px;
    transition: background-color var(--transition-fast);
  }

  .worktree-card:hover {
    background: var(--color-surface);
  }

  .worktree-rail {
    position: absolute;
    left: 19px;
    top: -1px;
    bottom: 50%;
    width: 1px;
    background: var(--color-border-strong);
  }

  .worktree-item:last-child .worktree-rail {
    bottom: 50%;
  }

  .worktree-card::before {
    content: "";
    position: absolute;
    left: 19px;
    top: 50%;
    width: 12px;
    height: 1px;
    background: var(--color-border-strong);
  }

  .worktree-dot {
    grid-column: 1;
    justify-self: end;
    width: 6px;
    height: 6px;
    margin-right: 2px;
    border-radius: 999px;
    background: var(--color-ink-faint);
    box-shadow: 0 0 0 3px var(--color-surface);
    z-index: 1;
  }

  .worktree-item.status-running .worktree-dot { background: var(--color-success); }
  .worktree-item.status-waiting .worktree-dot,
  .worktree-item.status-blocked .worktree-dot { background: var(--color-warning); }
  .worktree-item.status-failed .worktree-dot { background: var(--color-danger); }

  .worktree-card-body {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .worktree-card-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }

  .worktree-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--color-ink);
    font-size: var(--font-size-1);
    font-weight: 550;
    text-decoration: none;
  }

  .worktree-name:hover {
    color: var(--color-accent);
  }

  .queue-link {
    min-height: 24px;
    display: inline-flex;
    align-items: center;
    padding: 0 8px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 999px;
    color: var(--color-accent);
    font-size: 11px;
    font-weight: 650;
    text-decoration: none;
    white-space: nowrap;
  }

  .queue-link:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
  }

  .worktree-run-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 32ch;
  }

  /* Status accent strip on the left edge of the repo card */
  .repo-item::before {
    content: "";
    position: absolute;
    left: 0;
    top: 12px;
    bottom: 12px;
    width: 3px;
    border-radius: 0 3px 3px 0;
    background: transparent;
    transition: background-color var(--transition-base);
  }
  .repo-item.status-running::before { background: var(--color-success); }
  .repo-item.status-waiting::before,
  .repo-item.status-blocked::before { background: var(--color-warning); }
  .repo-item.status-failed::before { background: var(--color-danger); }

  @media (max-width: 760px) {
    .repos-hero {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
    }
    .repos-hero-stats {
      align-self: flex-start;
    }
    .repo-card {
      grid-template-columns: auto minmax(0, 1fr);
      grid-template-rows: auto auto;
      row-gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
    }
    .repo-card-counts {
      grid-column: 1 / -1;
      flex-wrap: wrap;
    }
    .repo-chevron {
      display: none;
    }
    .worktree-list {
      padding: 0 var(--space-4) var(--space-3);
    }
    .worktree-card {
      grid-template-columns: 24px minmax(0, 1fr);
      grid-template-rows: auto auto;
      row-gap: 4px;
    }
    .worktree-card-counts {
      grid-column: 2;
    }
  }
</style>
