<script lang="ts">
  import type {
    RepoWorktreeIndexFilter,
    RepoWorktreeDetailViewModel,
    RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';
  import { countRepoWorktreeIndexEntities, filterRepoWorktreeIndexRows, rowRelativeTime, visibleRepoWorktreeChildren } from '$lib/viewModels/repoWorktree';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { PartialPageIssue } from '$lib/api/client';
  import PageHero from './PageHero.svelte';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';

  let {
    state: viewState,
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
  const contextspaceIssues = $derived(sectionIssues.filter((issue) => issue.id === 'contextspace'));
  const artifactIssues = $derived(sectionIssues.filter((issue) => issue.id === 'artifacts'));
  const queueTickets = $derived(detail ? [...detail.currentTickets, ...detail.nextTickets] : []);

  function formatGitDiff(insertions: number | null, deletions: number | null): string | null {
    const parts: string[] = [];
    if (insertions && insertions > 0) parts.push(`+${insertions}`);
    if (deletions && deletions > 0) parts.push(`-${deletions}`);
    return parts.length ? parts.join(' ') : null;
  }

  function pluralize(count: number, singular: string, plural?: string): string {
    return `${count} ${count === 1 ? singular : plural ?? `${singular}s`}`;
  }

  const shortDetailTitle = $derived.by(() => {
    if (!detail) return '';
    if (detail.kind === 'worktree' && detail.baseRepoLabel) {
      const prefix = `${detail.baseRepoLabel}--`;
      if (detail.title.startsWith(prefix)) return detail.title.slice(prefix.length);
    }
    return detail.title;
  });

  const detailSubtitle = $derived.by(() => {
    if (!detail) return '';
    const parts: string[] = [];
    if (detail.branch) parts.push(detail.branch);
    if (detail.path) parts.push(detail.path);
    return parts.join(' · ');
  });

  const showFlowStrip = $derived.by(() => {
    if (!detail) return false;
    const f = detail.flowStatus;
    const hasSignal = f.signal !== 'idle' && f.signal !== 'done';
    const hasTicket = f.currentTicketLabel !== 'None';
    const hasTurns = f.turnsLabel !== 'Unknown';
    const hasElapsed = f.elapsedLabel !== 'Unknown';
    const hasProgress = f.progressLabel !== '0/0';
    const hasActivity = f.lastActivityLabel !== 'No activity yet';
    const hasReason = f.reasonLabel !== 'No reason reported';
    return hasSignal || hasTicket || hasTurns || hasElapsed || hasProgress || hasActivity || hasReason;
  });

  const REPO_FILTERS: RepoWorktreeIndexFilter[] = ['all', 'waiting', 'active'];
  let search = $state('');
  let filter = $state<RepoWorktreeIndexFilter>('all');

  const indexRows = $derived(index?.rows ?? []);

  const filteredRows = $derived(filterRepoWorktreeIndexRows(indexRows, search, filter));

  function visibleChildren(row: RepoWorktreeIndexViewModel['rows'][number]) {
    return visibleRepoWorktreeChildren(row, search, filter);
  }

  function repoFilterCount(key: RepoWorktreeIndexFilter): number {
    if (key === 'all') return countRepoWorktreeIndexEntities(indexRows);
    if (key === 'active') return index?.activeCount ?? 0;
    return index?.waitingCount ?? 0;
  }

  function repoFilterLabel(key: RepoWorktreeIndexFilter): string {
    return key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1);
  }
</script>

{#if viewState === 'loading'}
  <section class="page-stack">
    <div class="state-panel">Loading workspace state...</div>
  </section>
{:else if viewState === 'error'}
  <section class="page-stack">
    <div class="state-panel error">Could not load workspace state. {errorMessage}</div>
  </section>
{:else if mode === 'index' && index}
  <section class="page-stack repo-worktree-page repos-index-v2">
    {#if indexRows.length > 0}
      <header class="repos-controls">
        <label class="search-field repos-search">
          <span class="sr-only">Search repos and worktrees</span>
          <input bind:value={search} type="search" placeholder="Search repos, worktrees, branches" />
        </label>
        <div class="filter-row" aria-label="Repo status filters">
          {#each REPO_FILTERS as item}
            <button
              class:active={filter === item}
              class="chip"
              type="button"
              onclick={() => (filter = item)}
            >
              {repoFilterLabel(item)}
              <span>{repoFilterCount(item)}</span>
            </button>
          {/each}
        </div>
      </header>
    {/if}

    {@render degradedIssues(currentRunIssues)}
    {@render degradedIssues(ticketIssues)}

    {#if indexRows.length === 0}
      <div class="state-panel empty-state compact-empty repos-empty">
        <strong>No repos registered</strong>
        <p>Register a workspace before queueing repo-scoped tickets.</p>
      </div>
    {:else if filteredRows.length === 0}
      <div class="state-panel empty-state compact-empty repos-empty">
        <strong>No matches</strong>
        <p>Try a different search or filter.</p>
      </div>
    {:else}
      <ul class="repos-list" role="list">
        {#each filteredRows as row}
          {@const accent = repoAccent(row.label)}
          <li class={`repo-item status-${row.status}`} class:has-children={row.childWorktrees.length > 0} style={`--repo-accent: ${accent};`}>
            <a class="repo-card" href={href(row.href)}>
              <span class="repo-avatar" aria-hidden="true">{repoInitials(row.label)}</span>
              <div class="repo-card-body">
                <div class="repo-card-title">
                  <span class="repo-name">{row.label}</span>
                  {#if row.status !== 'idle' && row.status !== 'done'}
                    <span class={`repo-status status-pill ${row.status}`}>{statusLabel(row.status)}</span>
                  {/if}
                </div>
                <div class="repo-card-meta">
                  {#if row.branch}
                    <span class="repo-meta-branch">{row.branch}</span>
                  {/if}
                  {#if row.detail}
                    {#if row.branch}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                    <span>{row.detail}</span>
                  {/if}
                  {#if row.lastActivityAt}
                    {#if row.branch || row.detail}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                    <span class="repo-meta-time">{rowRelativeTime(row)}</span>
                  {/if}
                </div>
              </div>
              {#if row.activeRuns > 0 || row.openTickets > 0}
                <div class="repo-card-counts" aria-label="Activity counts">
                  {#if row.activeRuns > 0}
                    <span class="count-chip is-active" title="Active runs">
                      <strong>{row.activeRuns}</strong><em>run{row.activeRuns === 1 ? '' : 's'}</em>
                    </span>
                  {/if}
                  {#if row.openTickets > 0}
                    <span class="count-chip is-tickets" title="Open tickets">
                      <strong>{row.openTickets}</strong><em>ticket{row.openTickets === 1 ? '' : 's'}</em>
                    </span>
                  {/if}
                </div>
              {/if}
            </a>
            {#if row.signalWaiting > 0 || row.signalFailed > 0 || row.signalActive > 0}
              <div class="repo-row-toolbar">
                <div class="repo-signal-pills" aria-label="Scoped PMA chats and runs">
                  {#if row.signalWaiting > 0}<span class="signal-pill waiting">{row.signalWaiting} waiting</span>{/if}
                  {#if row.signalFailed > 0}<span class="signal-pill failed">{row.signalFailed} failed</span>{/if}
                  {#if row.signalActive > 0}<span class="signal-pill active">{row.signalActive} active</span>{/if}
                </div>
              </div>
            {/if}

            {#if visibleChildren(row).length > 0}
              <ul class="worktree-list" role="list" aria-label={`Worktrees owned by ${row.label}`}>
                {#each visibleChildren(row) as worktree}
                  <li class={`worktree-item status-${worktree.status}`}>
                    <div class="worktree-card">
                      <span class="worktree-rail" aria-hidden="true"></span>
                      <span class="worktree-dot" aria-hidden="true"></span>
                      <div class="worktree-card-body">
                        <div class="worktree-card-title">
                          <a class="worktree-name" href={href(worktree.href)}>{worktree.label}</a>
                          {#if worktree.status !== 'idle' && worktree.status !== 'done'}
                            <span class={`status-pill ${worktree.status}`}>{statusLabel(worktree.status)}</span>
                          {/if}
                        </div>
                        {#if worktree.branch || worktree.currentRunTitle}
                          <div class="worktree-card-meta">
                            {#if worktree.branch}
                              <span class="repo-meta-branch">{worktree.branch}</span>
                            {/if}
                            {#if worktree.currentRunTitle}
                              {#if worktree.branch}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                              <span class="worktree-run-title">{worktree.currentRunTitle}</span>
                            {/if}
                          </div>
                        {/if}
                      </div>
                      {#if worktree.activeRuns > 0 || worktree.openTickets > 0 || worktree.signalWaiting > 0 || worktree.signalFailed > 0 || worktree.signalActive > 0}
                        <div class="worktree-card-counts">
                          {#if worktree.signalWaiting > 0}
                            <span class="signal-pill waiting" title="Scoped PMA chats or runs waiting for attention">{worktree.signalWaiting} waiting</span>
                          {/if}
                          {#if worktree.signalFailed > 0}
                            <span class="signal-pill failed" title="Scoped PMA chats or runs failed">{worktree.signalFailed} failed</span>
                          {/if}
                          {#if worktree.signalActive > 0}
                            <span class="signal-pill active" title="Scoped PMA chats or runs active">{worktree.signalActive} active</span>
                          {/if}
                          {#if worktree.activeRuns > 0}
                            <span class="count-chip is-active" title="Active runs">
                              <strong>{worktree.activeRuns}</strong><em>run{worktree.activeRuns === 1 ? '' : 's'}</em>
                            </span>
                          {/if}
                          {#if worktree.openTickets > 0}
                            {#if worktree.ticketHref}
                              <a
                                class="count-chip count-chip-link is-tickets"
                                href={href(worktree.ticketHref)}
                                title="Open worktree tickets"
                              >
                                <strong>{worktree.openTickets}</strong><em>ticket{worktree.openTickets === 1 ? '' : 's'}</em>
                              </a>
                            {:else}
                              <span class="count-chip is-tickets" title="Open worktree tickets">
                                <strong>{worktree.openTickets}</strong><em>ticket{worktree.openTickets === 1 ? '' : 's'}</em>
                              </span>
                            {/if}
                          {/if}
                        </div>
                      {/if}
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
      <PageHero
        title={detail.title}
        subtitle={`Route id ${detail.id} does not match a known ${detail.kind} in the current hub inventory.`}
      >
        {#snippet actions()}
          <a class="hero-action" href={href(detail.missingIndexHref)}>{detail.missingIndexLabel}</a>
        {/snippet}
      </PageHero>

      <section class="page-panel identity-panel">
        <h2>Unknown workspace</h2>
        <div class="state-panel empty-state compact-empty">
          <strong>No matching {detail.kind}</strong>
          <p>Refresh the workspace inventory or choose a known {detail.kind} from the index before opening scoped tickets, runs, or contextspace.</p>
        </div>
      </section>
    {:else}
    <PageHero title={shortDetailTitle} subtitle={detailSubtitle} />

    {#if detail.gitStatus}
      {@const git = detail.gitStatus}
      {@const diffLabel = formatGitDiff(git.insertions, git.deletions)}
      <div class="git-status-bar" aria-label="Git status">
        <span class={`git-state-pill ${git.dirty ? 'dirty' : 'clean'}`}>
          <span class="git-state-dot" aria-hidden="true"></span>
          {git.dirty ? 'Dirty' : 'Clean'}
        </span>
        {#if git.filesChanged !== null && git.filesChanged > 0}
          <span class="git-chip">{pluralize(git.filesChanged, 'file')} changed</span>
        {/if}
        {#if diffLabel}
          <span class="git-chip git-chip-diff">{diffLabel}</span>
        {/if}
        {#if git.staged !== null && git.staged > 0}
          <span class="git-chip">{git.staged} staged</span>
        {/if}
        {#if git.untracked !== null && git.untracked > 0}
          <span class="git-chip">{git.untracked} untracked</span>
        {/if}
        {#if git.hasUpstream === false}
          <span class="git-chip git-chip-warn">No upstream</span>
        {:else}
          {#if git.ahead !== null && git.ahead > 0}
            <span class="git-chip git-chip-ahead">↑ {git.ahead} ahead</span>
          {/if}
          {#if git.behind !== null && git.behind > 0}
            <span class="git-chip git-chip-behind">↓ {git.behind} behind</span>
          {/if}
        {/if}
      </div>
    {/if}

    {#if showFlowStrip}
      <section class={`ticket-flow-strip ${detail.flowStatus.signal}`} aria-label="Ticket flow status">
        {#if detail.flowStatus.signal !== 'idle' && detail.flowStatus.signal !== 'done'}
          <div>
            <span>Status</span>
            <strong>{detail.flowStatus.statusLabel}</strong>
          </div>
        {/if}
        {#if detail.flowStatus.currentTicketLabel !== 'None'}
          <div>
            <span>Current ticket</span>
            {#if detail.flowStatus.currentTicketHref}
              <a href={href(detail.flowStatus.currentTicketHref)}>{detail.flowStatus.currentTicketLabel}</a>
            {:else}
              <strong>{detail.flowStatus.currentTicketLabel}</strong>
            {/if}
          </div>
        {/if}
        {#if detail.flowStatus.turnsLabel !== 'Unknown'}
          <div><span>Turns</span><strong>{detail.flowStatus.turnsLabel}</strong></div>
        {/if}
        {#if detail.flowStatus.elapsedLabel !== 'Unknown'}
          <div><span>Elapsed</span><strong>{detail.flowStatus.elapsedLabel}</strong></div>
        {/if}
        {#if detail.flowStatus.progressLabel !== '0/0'}
          <div><span>Done/total</span><strong>{detail.flowStatus.progressLabel}</strong></div>
        {/if}
        {#if detail.flowStatus.lastActivityLabel !== 'No activity yet'}
          <div><span>Last activity</span><strong>{detail.flowStatus.lastActivityLabel}</strong></div>
        {/if}
        {#if detail.flowStatus.reasonLabel !== 'No reason reported'}
          <div class="flow-reason"><span>Reason</span><strong>{detail.flowStatus.reasonLabel}</strong></div>
        {/if}
      </section>
    {/if}

    <div class="detail-grid">
      {#if detail.hasActiveRun && detail.currentRuns.length > 0}
        <section class="page-panel execution-panel wide">
          <div class="panel-heading-row">
            <h2>Active run</h2>
          </div>
          {@render degradedIssues(currentRunIssues)}
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
              {#if run.progress !== null}
                <span class="progress-track" aria-label={`${run.progress} percent complete`}>
                  <span style={`width: ${run.progress}%`}></span>
                </span>
              {/if}
              <div class="row-links">
                {#if run.chatHref}<a href={href(run.chatHref)}>PMA chat</a>{/if}
                {#if run.ticketHref}<a href={href(run.ticketHref)}>Ticket</a>{/if}
              </div>
            </article>
          {/each}
        </section>
      {/if}

      <section class="page-panel execution-panel wide contextspace-panel">
        <div class="panel-heading-row">
          <h2>Contextspace</h2>
          <a href={href(detail.contextspaceHref)}>Open</a>
        </div>
        {@render degradedIssues(contextspaceIssues)}
        <ul class="contextspace-compact-list" role="list">
          {#each detail.contextspace as doc}
            <li class={`contextspace-compact-item ${doc.status}`} class:has-preview={Boolean(doc.preview)}>
              <a class="contextspace-compact-row" href={href(doc.href)}>
                <span class={`contextspace-compact-dot ${doc.status}`} aria-hidden="true"></span>
                <span class="contextspace-compact-name">{doc.filename}</span>
                <span class="contextspace-compact-summary">{doc.summary}</span>
                {#if doc.updatedAt}
                  <span class="contextspace-compact-time">{rowRelativeTime({ updatedAt: doc.updatedAt })}</span>
                {/if}
              </a>
              {#if doc.previewHtml}
                <a class="contextspace-spec-preview" href={href(doc.href)} aria-label={`Open ${doc.filename}`}>
                  <div class="contextspace-spec-preview-body markdown-body">
                    {@html doc.previewHtml}
                  </div>
                  <span class="contextspace-spec-preview-fade" aria-hidden="true"></span>
                </a>
              {:else if doc.preview}
                <a class="contextspace-spec-preview" href={href(doc.href)}>
                  <pre>{doc.preview}</pre>
                </a>
              {/if}
            </li>
          {/each}
        </ul>
      </section>

      <section class="page-panel execution-panel wide workspace-ticket-queue-panel">
        <div class="panel-heading-row">
          <h2>{detail.kind === 'worktree' ? 'Worktree tickets' : 'Repo tickets'}</h2>
          <a href={href(detail.ticketIndexHref)}>All</a>
        </div>
        {@render degradedIssues(ticketIssues)}
        {#if detail.ticketOverview.total > 0}
          <div class="ticket-overview-stats" aria-label="Ticket overview">
            <div><span>Open</span><strong>{detail.ticketOverview.open}</strong></div>
            <div><span>Done/total</span><strong>{detail.ticketOverview.done}/{detail.ticketOverview.total}</strong></div>
            {#if detail.ticketOverview.active > 0}
              <div class="is-active"><span>Active</span><strong>{detail.ticketOverview.active}</strong></div>
            {/if}
            {#if detail.ticketOverview.failed > 0}
              <div class="is-failed"><span>Needs fix</span><strong>{detail.ticketOverview.failed}</strong></div>
            {/if}
          </div>
        {/if}
        <div class="workspace-ticket-list">
          {#if detail.ticketOverview.preview.length > 0}
            {#each detail.ticketOverview.preview as ticket}
              <a class={`workspace-ticket-row ${ticket.status}`} class:current={ticket.isCurrent} class:done={ticket.status === 'done'} href={href(ticket.href)}>
                <span>
                  <strong>{ticket.title}</strong>
                  {#if ticket.isCurrent}<em class="working-badge">Working</em>{/if}
                  {#if ticket.diffLabel}<em>{ticket.diffLabel}</em>{/if}
                  {#if ticket.durationLabel}<em>{ticket.durationLabel}</em>{/if}
                  {#if ticket.isCurrent && ticket.bodyPreview}<small>{ticket.bodyPreview}</small>{/if}
                </span>
                <span class="status-pill {ticket.status}">{statusLabel(ticket.status)}</span>
              </a>
            {/each}
            {#if detail.ticketOverview.remaining > 0}
              <a class="ticket-overview-more" href={href(detail.ticketIndexHref)}>
                +{detail.ticketOverview.remaining} more open ticket{detail.ticketOverview.remaining === 1 ? '' : 's'}
              </a>
            {/if}
          {:else if ticketIssues.length === 0}
            <div class="workspace-ticket-row empty-ticket-row" role="status">
              <span>
                <strong>No tickets</strong>
                <small>No scoped tickets are queued for this {detail.kind}.</small>
              </span>
              <span class="status-pill idle">idle</span>
            </div>
          {/if}
        </div>
      </section>

      <section class="page-panel execution-panel wide">
        <div class="panel-heading-row chats-panel-heading">
          <h2>Chats</h2>
          <div class="panel-heading-actions">
            <a class="hero-action" href={href(detail.pmaChatHref)}>PMA chat</a>
            <a class="hero-action" href={href(detail.codingAgentChatHref)}>Coding agent</a>
          </div>
        </div>
        {#if detail.chats.length > 0}
          <div class="chat-row-list">
            {#each detail.chats.slice(0, 5) as chat}
              {@const metaParts = [
                chat.agentId,
                chat.model,
                chat.updatedAt ? rowRelativeTime({ updatedAt: chat.updatedAt }) : null
              ].filter((p): p is string => typeof p === 'string' && p.length > 0)}
              <a class={`chat-row ${chat.status}`} href={href(chat.href)}>
                <span class={`chat-row-kind kind-${chat.kind}`}>{chat.kindLabel}</span>
                <span class="chat-row-body">
                  <span class="chat-row-title">{chat.title}</span>
                  {#if metaParts.length > 0}
                    <span class="chat-row-meta">{metaParts.join(' · ')}</span>
                  {/if}
                </span>
                {#if chat.status !== 'idle' && chat.status !== 'done'}
                  <span class="status-pill {chat.status}">{statusLabel(chat.status)}</span>
                {/if}
              </a>
            {/each}
            {#if detail.chats.length > 5}
              <a class="ticket-overview-more" href={href('/chats')}>+{detail.chats.length - 5} more chat{detail.chats.length - 5 === 1 ? '' : 's'}</a>
            {/if}
          </div>
        {/if}
      </section>

      {#if detail.artifacts.length > 0 || artifactIssues.length > 0}
        <section class="page-panel execution-panel wide">
          <h2>Surfaced artifacts</h2>
          {@render degradedIssues(artifactIssues)}
          {#if detail.artifacts.length > 0}
            {@render compactList(detail.artifacts, '')}
          {/if}
        </section>
      {/if}
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
        <a class="dashboard-row activity-row" href={href(item.href ?? '/chats')}>
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

  .repos-controls {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: 0 2px;
  }

  .repos-search {
    flex: 1 1 auto;
    min-width: 0;
  }

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
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-4) var(--space-5);
    color: var(--color-ink);
    text-decoration: none;
  }

  .repo-row-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-5) var(--space-3);
    border-top: 1px solid var(--color-border-subtle);
    background: var(--color-surface-muted);
  }

  .repo-signal-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .signal-pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
  }

  .signal-pill.waiting {
    color: var(--color-warning);
    border-color: color-mix(in srgb, var(--color-warning) 35%, var(--color-border));
  }

  .signal-pill.failed {
    color: var(--color-danger);
    border-color: color-mix(in srgb, var(--color-danger) 35%, var(--color-border));
  }

  .signal-pill.active {
    color: var(--color-success);
    border-color: color-mix(in srgb, var(--color-success) 35%, var(--color-border));
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

  .repo-meta-time {
    color: var(--color-ink-faint);
    font-variant-numeric: tabular-nums;
  }

  .repo-card-counts,
  .worktree-card-counts {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }

  .count-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-height: 24px;
    padding: 2px 10px;
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

  a.count-chip-link {
    text-decoration: none;
    transition: filter var(--transition-base), box-shadow var(--transition-base);
  }

  a.count-chip-link:hover {
    filter: brightness(0.95);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent) 20%, transparent);
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
    .repos-controls {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-2);
      padding: 0;
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

  .panel-heading-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .chat-row-list {
    display: grid;
    gap: var(--space-2);
  }

  .contextspace-row-list {
    display: grid;
    gap: var(--space-2);
  }

  .contextspace-row {
    display: grid;
    grid-template-columns: minmax(140px, auto) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border-radius: 8px;
    border: 1px solid var(--color-border-subtle);
    background: var(--color-surface);
    color: inherit;
    text-decoration: none;
    transition: border-color var(--transition-base), background var(--transition-base);
  }

  .contextspace-row:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-hover, var(--color-surface));
  }

  .contextspace-row-kind {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: var(--color-ink-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .contextspace-row-body {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .contextspace-row-title {
    font-weight: 600;
    color: var(--color-text-strong, inherit);
  }

  .contextspace-row-meta {
    font-size: 0.85rem;
    color: var(--color-text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workspace-ticket-row.empty-ticket-row {
    cursor: default;
  }

  .workspace-ticket-row.empty-ticket-row:hover {
    border-color: var(--color-border-subtle);
    background: var(--color-surface);
  }

  .chat-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border-radius: 8px;
    border: 1px solid var(--color-border-subtle);
    background: var(--color-surface);
    text-decoration: none;
    color: inherit;
    transition: border-color var(--transition-base), background var(--transition-base);
  }
  .chat-row:hover {
    border-color: var(--color-border-strong);
    background: var(--color-surface-hover, var(--color-surface));
  }
  .chat-row-kind {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    background: var(--color-surface-muted, rgb(255 255 255 / 0.04));
    color: var(--color-text-muted);
    border: 1px solid var(--color-border-subtle);
  }
  .chat-row-kind.kind-coding_agent {
    color: var(--color-accent);
    border-color: color-mix(in srgb, var(--color-accent) 40%, transparent);
  }
  .chat-row-body {
    display: grid;
    gap: 2px;
    min-width: 0;
  }
  .chat-row-title {
    font-weight: 600;
    color: var(--color-text-strong, inherit);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chat-row-meta {
    font-size: 0.85rem;
    color: var(--color-text-muted);
  }

  @media (max-width: 760px) {
    .contextspace-row {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .contextspace-row-kind {
      grid-column: 1 / -1;
    }
  }

  /* Git status bar — sits between hero and flow strip. */
  .git-status-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    padding: 0 2px;
  }

  .git-state-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 22px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.01em;
    border: 1px solid transparent;
  }

  .git-state-pill .git-state-dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: currentColor;
  }

  .git-state-pill.clean {
    background: var(--color-success-soft);
    color: var(--color-success);
    border-color: color-mix(in srgb, var(--color-success) 30%, transparent);
  }

  .git-state-pill.dirty {
    background: color-mix(in srgb, var(--color-warning) 14%, transparent);
    color: var(--color-warning);
    border-color: color-mix(in srgb, var(--color-warning) 35%, transparent);
  }

  .git-chip {
    display: inline-flex;
    align-items: center;
    height: 22px;
    padding: 0 9px;
    border-radius: 999px;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 11px;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    border: 1px solid var(--color-border-subtle);
    white-space: nowrap;
  }

  .git-chip-diff {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  }

  .git-chip-ahead {
    color: var(--color-accent);
    border-color: color-mix(in srgb, var(--color-accent) 32%, transparent);
    background: var(--color-accent-soft);
  }

  .git-chip-behind {
    color: var(--color-warning);
    border-color: color-mix(in srgb, var(--color-warning) 32%, transparent);
    background: color-mix(in srgb, var(--color-warning) 12%, transparent);
  }

  .git-chip-warn {
    color: var(--color-ink-muted);
    border-style: dashed;
  }

  /* Compact contextspace list */
  .contextspace-compact-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .contextspace-compact-item {
    display: flex;
    flex-direction: column;
    border-radius: 6px;
  }

  .contextspace-compact-row {
    display: grid;
    grid-template-columns: 10px minmax(120px, auto) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-3);
    padding: 8px 10px;
    color: inherit;
    text-decoration: none;
    border-radius: 6px;
    transition: background-color var(--transition-fast);
  }

  .contextspace-compact-row:hover {
    background: var(--color-surface-muted);
  }

  .contextspace-compact-dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--color-border-strong);
    justify-self: center;
  }

  .contextspace-compact-dot.present {
    background: var(--color-success);
  }

  .contextspace-compact-name {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: var(--color-ink-soft);
    white-space: nowrap;
  }

  .contextspace-compact-summary {
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  .contextspace-compact-item.empty .contextspace-compact-summary {
    color: var(--color-ink-faint);
    font-style: italic;
  }

  .contextspace-compact-time {
    font-size: 11px;
    color: var(--color-ink-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .contextspace-spec-preview {
    position: relative;
    display: block;
    margin: 6px 10px 10px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 8px;
    background: var(--color-surface-sunken);
    text-decoration: none;
    color: inherit;
    overflow: hidden;
    transition: border-color var(--transition-base);
  }

  .contextspace-spec-preview:hover {
    border-color: var(--color-border-strong);
  }

  .contextspace-spec-preview-body {
    padding: 14px 18px 18px;
    max-height: 360px;
    overflow: hidden;
    color: var(--color-ink);
    font-size: var(--font-size-1);
    line-height: 1.55;
  }

  .contextspace-spec-preview-body :global(h1),
  .contextspace-spec-preview-body :global(h2),
  .contextspace-spec-preview-body :global(h3) {
    margin: 0 0 8px;
    font-weight: 650;
    letter-spacing: -0.01em;
  }

  .contextspace-spec-preview-body :global(h1) { font-size: var(--font-size-2); }
  .contextspace-spec-preview-body :global(h2) { font-size: var(--font-size-1); margin-top: 14px; }
  .contextspace-spec-preview-body :global(h3) { font-size: var(--font-size-1); color: var(--color-ink-soft); margin-top: 10px; }

  .contextspace-spec-preview-body :global(p) {
    margin: 0 0 8px;
    color: var(--color-ink-soft);
  }

  .contextspace-spec-preview-body :global(ul) {
    margin: 0 0 8px;
    padding-left: 20px;
    color: var(--color-ink-soft);
  }

  .contextspace-spec-preview-body :global(li) {
    margin-bottom: 2px;
  }

  .contextspace-spec-preview-body :global(code) {
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.92em;
    padding: 1px 5px;
    border-radius: 4px;
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .contextspace-spec-preview-body :global(pre) {
    margin: 0 0 8px;
    padding: 10px 12px;
    border-radius: 6px;
    background: var(--color-surface-muted);
    overflow: auto;
    font-size: 12px;
  }

  .contextspace-spec-preview-body :global(pre code) {
    background: transparent;
    padding: 0;
  }

  .contextspace-spec-preview-body :global(strong) {
    color: var(--color-ink);
  }

  .contextspace-spec-preview-body :global(a) {
    color: var(--color-accent);
    text-decoration: none;
  }

  .contextspace-spec-preview-fade {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 56px;
    pointer-events: none;
    background: linear-gradient(180deg, transparent, var(--color-surface-sunken));
  }

  .contextspace-spec-preview pre {
    margin: 0;
    padding: 10px 14px;
    max-height: 220px;
    overflow: auto;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-ink-soft);
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* Ticket overview compact stats */
  .ticket-overview-stats {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3) var(--space-5);
    padding: 4px 2px var(--space-2);
    border-bottom: 1px dashed var(--color-border-subtle);
    margin-bottom: var(--space-2);
  }

  .ticket-overview-stats > div {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
  }

  .ticket-overview-stats span {
    color: var(--color-ink-muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .ticket-overview-stats strong {
    color: var(--color-ink);
    font-size: var(--font-size-2);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .ticket-overview-stats .is-active strong { color: var(--color-success); }
  .ticket-overview-stats .is-failed strong { color: var(--color-danger); }

  .ticket-overview-more {
    align-self: flex-start;
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid var(--color-border-subtle);
    background: transparent;
    font-size: 12px;
    color: var(--color-ink-muted);
    text-decoration: none;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }

  .ticket-overview-more:hover {
    color: var(--color-accent);
    border-color: var(--color-accent);
  }
</style>
