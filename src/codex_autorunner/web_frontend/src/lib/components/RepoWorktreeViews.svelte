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
  import TicketDiffStats from './TicketDiffStats.svelte';
  import VirtualList from '$lib/components/VirtualList.svelte';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';

  let {
    state: viewState,
    mode,
    index = null,
    detail = null,
    errorMessage = null,
    sectionIssues = [],
    onRetry = undefined,
    onCleanupWorktree = undefined,
    onArchiveState = undefined,
    onSyncRepo = undefined,
    syncRepoBusy = false,
    onCreateRepo = undefined,
    onCreateWorktree = undefined,
    onOpenRepoSettings = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    mode: 'index' | 'detail';
    index?: RepoWorktreeIndexViewModel | null;
    detail?: RepoWorktreeDetailViewModel | null;
    errorMessage?: string | null;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
    onCleanupWorktree?: ((worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }) => void | Promise<void>) | undefined;
    onArchiveState?: ((target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }) => void | Promise<void>) | undefined;
    onSyncRepo?: (() => void | Promise<void>) | undefined;
    syncRepoBusy?: boolean;
    onCreateRepo?: (() => void) | undefined;
    onCreateWorktree?: ((target: { id: string; label: string }) => void) | undefined;
    onOpenRepoSettings?: ((target: { id: string; label: string; worktreeSetupCommands: string[] }) => void) | undefined;
  } = $props();

  const currentRunIssues = $derived(sectionIssues.filter((issue) => issue.id === 'current_run'));
  const ticketIssues = $derived(sectionIssues.filter((issue) => issue.id === 'tickets'));
  const contextspaceIssues = $derived(sectionIssues.filter((issue) => issue.id === 'contextspace'));
  const artifactIssues = $derived(sectionIssues.filter((issue) => issue.id === 'artifacts'));
  const queueTickets = $derived(detail ? [...detail.currentTickets, ...detail.nextTickets] : []);

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

  // Per-repo collapse state. Tracks IDs whose state DIFFERS from the global default.
  let globalCollapsed = $state(false);
  let toggledRepoIds = $state<Record<string, true>>({});

  function isRepoCollapsed(repoId: string): boolean {
    const flipped = toggledRepoIds[repoId] === true;
    return flipped ? !globalCollapsed : globalCollapsed;
  }

  function toggleRepoCollapsed(repoId: string): void {
    if (toggledRepoIds[repoId]) {
      const next = { ...toggledRepoIds };
      delete next[repoId];
      toggledRepoIds = next;
    } else {
      toggledRepoIds = { ...toggledRepoIds, [repoId]: true };
    }
  }

  function setAllCollapsed(value: boolean): void {
    globalCollapsed = value;
    toggledRepoIds = {};
  }

  const indexRows = $derived(index?.rows ?? []);
  const ticketMetricsReady = $derived(index?.ticketIndexMetricsAvailable ?? false);

  const filteredRows = $derived(filterRepoWorktreeIndexRows(indexRows, search, filter));

  const collapsibleRepoCount = $derived(
    indexRows.filter((row) => row.kind === 'repo' && row.totalWorktrees > 0).length
  );

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

  function canArchiveState(target: { hasCarState: boolean; unboundManagedThreadCount: number }): boolean {
    return target.hasCarState || target.unboundManagedThreadCount > 0;
  }

  function handleCleanupClick(
    event: MouseEvent,
    worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onCleanupWorktree?.(worktree);
  }

  function handleArchiveClick(
    event: MouseEvent,
    target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onArchiveState?.(target);
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
        <div class="repos-controls-row">
          <label class="search-field repos-search">
            <span class="sr-only">Search repos and worktrees</span>
            <input bind:value={search} type="search" placeholder="Search repos, worktrees, branches" />
          </label>
          {#if onCreateRepo}
            <button class="new-chat-button" type="button" onclick={() => onCreateRepo?.()}>
              + New repo
            </button>
          {/if}
        </div>
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
          {#if collapsibleRepoCount > 0}
            <button
              class="chip collapse-all-chip"
              type="button"
              title={globalCollapsed ? 'Expand all repos' : 'Collapse all repos'}
              aria-label={globalCollapsed ? 'Expand all repos' : 'Collapse all repos'}
              onclick={() => setAllCollapsed(!globalCollapsed)}
            >
              {#if globalCollapsed}
                {@render expandAllIcon()}
                <span>Expand all</span>
              {:else}
                {@render collapseAllIcon()}
                <span>Collapse all</span>
              {/if}
            </button>
          {/if}
        </div>
      </header>
    {/if}

    {@render degradedIssues(currentRunIssues)}
    {@render degradedIssues(ticketIssues)}

    {#if indexRows.length === 0}
      <div class="state-panel empty-state compact-empty repos-empty">
        <strong>No repos registered</strong>
        <p>Register a workspace before queueing repo-scoped tickets.</p>
        {#if onCreateRepo}
          <button class="new-chat-button" type="button" onclick={() => onCreateRepo?.()}>
            + New repo
          </button>
        {/if}
      </div>
    {:else if filteredRows.length === 0}
      <div class="state-panel empty-state compact-empty repos-empty">
        <strong>No matches</strong>
        <p>Try a different search or filter.</p>
      </div>
    {:else}
      <VirtualList
        items={filteredRows}
        key={(row) => row.id}
        estimatedItemSize={122}
        overscan={8}
        initialCount={40}
        ariaLabel="Repo and worktree index"
        class="repos-list"
      >
        {#snippet children(row)}
          {@const accent = repoAccent(row.label)}
          {@const collapsible = row.kind === 'repo' && row.totalWorktrees > 0}
          {@const collapsed = collapsible && isRepoCollapsed(row.id)}
          <div class={`repo-item status-${row.status}`} class:has-children={row.childWorktrees.length > 0} class:is-collapsed={collapsed} role="listitem" style={`--repo-accent: ${accent};`}>
            <div class="repo-head">
            {#if collapsible}
              <button
                class="repo-collapse-toggle"
                type="button"
                aria-expanded={!collapsed}
                aria-label={collapsed ? `Expand worktrees for ${row.label}` : `Collapse worktrees for ${row.label}`}
                title={collapsed ? 'Show worktrees' : 'Hide worktrees'}
                onclick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  toggleRepoCollapsed(row.id);
                }}
              >
                {@render chevronIcon()}
              </button>
            {/if}
            <div class="repo-card">
              <a class="repo-card-main" href={href(row.href)} aria-label={`Open ${row.label} detail`}>
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
              </a>
              {#if row.activeRuns > 0 || (ticketMetricsReady && row.openTickets > 0) || (collapsed && row.totalWorktrees > 0)}
                <div class="repo-card-counts" aria-label="Activity counts">
                  {#if collapsed && row.totalWorktrees > 0}
                    {@const dirtyLabel = row.dirtyWorktrees > 0 ? `${row.dirtyWorktrees} dirty, ` : ''}
                    <span
                      class="count-chip is-in-use"
                      class:idle={row.inUseWorktrees === 0}
                      title={`${dirtyLabel}${row.inUseWorktrees} of ${row.totalWorktrees} worktrees in use`}
                    >
                      <strong>{row.inUseWorktrees}</strong><em>/{row.totalWorktrees} in use</em>
                    </span>
                  {/if}
                  {#if row.activeRuns > 0}
                    <a
                      class="count-chip count-chip-link is-active"
                      href={href(row.href)}
                      title="Open runs and execution state"
                    >
                      <strong>{row.activeRuns}</strong><em>run{row.activeRuns === 1 ? '' : 's'}</em>
                    </a>
                  {/if}
                  {#if row.openTickets > 0}
                    {#if row.ticketHref}
                      <a
                        class="count-chip count-chip-link is-tickets"
                        href={href(row.ticketHref)}
                        title={row.totalTickets > 0 ? `${row.doneTickets} of ${row.totalTickets} done` : 'Open tickets'}
                      >
                        <strong>{row.openTickets}</strong><em>ticket{row.openTickets === 1 ? '' : 's'}</em>
                        {#if row.totalTickets > 0 && row.doneTickets > 0}
                          <span class="count-chip-progress">{row.doneTickets}/{row.totalTickets}</span>
                        {/if}
                      </a>
                    {:else}
                      <span class="count-chip is-tickets" title={row.totalTickets > 0 ? `${row.doneTickets} of ${row.totalTickets} done` : 'Open tickets'}>
                        <strong>{row.openTickets}</strong><em>ticket{row.openTickets === 1 ? '' : 's'}</em>
                        {#if row.totalTickets > 0 && row.doneTickets > 0}
                          <span class="count-chip-progress">{row.doneTickets}/{row.totalTickets}</span>
                        {/if}
                      </span>
                    {/if}
                  {/if}
                </div>
              {/if}
            </div>
            <div class="repo-action-buttons repo-head-actions" aria-label={`Actions for ${row.label}`}>
              <a
                class="row-action-button row-action-link"
                href={href(row.pmaChatHref)}
                title={`Start a new PMA chat scoped to ${row.label}`}
                aria-label={`Start PMA chat for ${row.label}`}
                data-sveltekit-preload-data="tap"
              >
                + Chat
              </a>
              <a
                class="row-action-button row-action-link"
                href={href(row.codingAgentChatHref)}
                title={`Start a new coding agent chat scoped to ${row.label}`}
                aria-label={`Start coding agent chat for ${row.label}`}
                data-sveltekit-preload-data="tap"
              >
                + Agent
              </a>
                {#if row.kind === 'repo' && onOpenRepoSettings}
                  <button
                    class="icon-action settings"
                    type="button"
                    title="Repo settings (worktree setup, etc.)"
                    aria-label={`Settings for ${row.label}`}
                    onclick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      onOpenRepoSettings?.({
                        id: row.id,
                        label: row.label,
                        worktreeSetupCommands: row.worktreeSetupCommands ?? []
                      });
                    }}
                  >
                    <span class="emoji-icon" aria-hidden="true">⚙️</span>
                  </button>
                {/if}
                {#if row.kind === 'repo' && onCreateWorktree}
                  <button
                    class="row-action-button"
                    type="button"
                    title="Create a new worktree from a fresh origin/main"
                    aria-label={`Create worktree on ${row.label}`}
                    onclick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      onCreateWorktree?.({ id: row.id, label: row.label });
                    }}
                  >
                    + Worktree
                  </button>
                {/if}
                {#if onArchiveState && canArchiveState(row)}
                  <button
                    class="icon-action archive"
                    type="button"
                    title="Archive CAR state without deleting git files"
                    aria-label={`Archive CAR state for ${row.label}`}
                    onclick={(event) => handleArchiveClick(event, {
                      kind: row.kind,
                      id: row.id,
                      label: row.label,
                      hasCarState: row.hasCarState,
                      unboundManagedThreadCount: row.unboundManagedThreadCount
                    })}
                  >
                    <span class="emoji-icon" aria-hidden="true">🧹</span>
                  </button>
                {/if}
                {#if row.kind === 'worktree' && onCleanupWorktree}
                  <button
                    class="icon-action cleanup"
                    type="button"
                    title="Cleanup worktree: archive a snapshot, then delete the checkout"
                    aria-label={`Cleanup worktree ${row.label}`}
                    onclick={(event) => handleCleanupClick(event, row)}
                  >
                    {@render trashIcon()}
                  </button>
                {/if}
              </div>
            {#if ticketMetricsReady && row.totalTickets > 0}
              {@const pct = Math.round((row.doneTickets / row.totalTickets) * 100)}
              <span
                class="ticket-progress ticket-progress--repo-head"
                title={`${row.doneTickets}/${row.totalTickets} tickets done (${pct}%)`}
                aria-hidden="true"
                style:--progress={`${pct}%`}
              ></span>
            {/if}
            </div>
            {#if row.signalWaiting > 0 || row.signalFailed > 0 || row.signalActive > 0}
              <div class="repo-signal-pills" aria-label="Scoped chats and runs">
                {#if row.signalWaiting > 0}<span class="signal-pill waiting">{row.signalWaiting} waiting</span>{/if}
                {#if row.signalFailed > 0}<span class="signal-pill failed">{row.signalFailed} failed</span>{/if}
                {#if row.signalActive > 0}<span class="signal-pill active">{row.signalActive} active</span>{/if}
              </div>
            {/if}

            {#if !collapsed && visibleChildren(row).length > 0}
              {@const childRows = visibleChildren(row)}
              <VirtualList
                items={childRows}
                key={(worktree) => worktree.id}
                estimatedItemSize={86}
                overscan={6}
                initialCount={32}
                ariaLabel={`Worktrees owned by ${row.label}`}
                class="worktree-list"
              >
                {#snippet children(worktree)}
                  <div class={`worktree-item status-${worktree.status}`} role="listitem">
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
                      {#if worktree.activeRuns > 0 || (ticketMetricsReady && worktree.openTickets > 0) || worktree.signalWaiting > 0 || worktree.signalFailed > 0 || worktree.signalActive > 0}
                        <div class="worktree-card-counts">
                          {#if worktree.signalWaiting > 0}
                            <span class="signal-pill waiting" title="Scoped chats or runs waiting for attention">{worktree.signalWaiting} waiting</span>
                          {/if}
                          {#if worktree.signalFailed > 0}
                            <span class="signal-pill failed" title="Scoped chats or runs failed">{worktree.signalFailed} failed</span>
                          {/if}
                          {#if worktree.signalActive > 0}
                            <span class="signal-pill active" title="Scoped chats or runs active">{worktree.signalActive} active</span>
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
                                title={worktree.totalTickets > 0 ? `${worktree.doneTickets} of ${worktree.totalTickets} done` : 'Open worktree tickets'}
                              >
                                <strong>{worktree.openTickets}</strong><em>ticket{worktree.openTickets === 1 ? '' : 's'}</em>
                                {#if worktree.totalTickets > 0 && worktree.doneTickets > 0}
                                  <span class="count-chip-progress">{worktree.doneTickets}/{worktree.totalTickets}</span>
                                {/if}
                              </a>
                            {:else}
                              <span class="count-chip is-tickets" title={worktree.totalTickets > 0 ? `${worktree.doneTickets} of ${worktree.totalTickets} done` : 'Open worktree tickets'}>
                                <strong>{worktree.openTickets}</strong><em>ticket{worktree.openTickets === 1 ? '' : 's'}</em>
                                {#if worktree.totalTickets > 0 && worktree.doneTickets > 0}
                                  <span class="count-chip-progress">{worktree.doneTickets}/{worktree.totalTickets}</span>
                                {/if}
                              </span>
                            {/if}
                          {/if}
                        </div>
                      {/if}
                      {#if ticketMetricsReady && worktree.totalTickets > 0}
                        {@const wpct = Math.round((worktree.doneTickets / worktree.totalTickets) * 100)}
                        <span
                          class="ticket-progress ticket-progress--worktree"
                          title={`${worktree.doneTickets}/${worktree.totalTickets} tickets done (${wpct}%)`}
                          aria-hidden="true"
                          style:--progress={`${wpct}%`}
                        ></span>
                      {/if}
                      <div class="repo-action-buttons" aria-label={`Actions for ${worktree.label}`}>
                        <a
                          class="row-action-button row-action-link"
                          href={href(worktree.pmaChatHref)}
                          title={`Start a new PMA chat scoped to ${worktree.label}`}
                          aria-label={`Start PMA chat for ${worktree.label}`}
                          data-sveltekit-preload-data="tap"
                        >
                          + Chat
                        </a>
                        <a
                          class="row-action-button row-action-link"
                          href={href(worktree.codingAgentChatHref)}
                          title={`Start a new coding agent chat scoped to ${worktree.label}`}
                          aria-label={`Start coding agent chat for ${worktree.label}`}
                          data-sveltekit-preload-data="tap"
                        >
                          + Agent
                        </a>
                          {#if onArchiveState && canArchiveState(worktree)}
                            <button
                              class="icon-action archive"
                              type="button"
                              title="Archive CAR state without deleting git files"
                              aria-label={`Archive CAR state for ${worktree.label}`}
                              onclick={(event) => handleArchiveClick(event, {
                                kind: 'worktree',
                                id: worktree.id,
                                label: worktree.label,
                                hasCarState: worktree.hasCarState,
                                unboundManagedThreadCount: worktree.unboundManagedThreadCount
                              })}
                            >
                              <span class="emoji-icon" aria-hidden="true">🧹</span>
                            </button>
                          {/if}
                          {#if onCleanupWorktree}
                            <button
                              class="icon-action cleanup"
                              type="button"
                              title="Cleanup worktree: archive a snapshot, then delete the checkout"
                              aria-label={`Cleanup worktree ${worktree.label}`}
                              onclick={(event) => handleCleanupClick(event, worktree)}
                            >
                              {@render trashIcon()}
                            </button>
                          {/if}
                        </div>
                    </div>
                  </div>
                {/snippet}
              </VirtualList>
            {/if}
          </div>
        {/snippet}
      </VirtualList>
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
    <PageHero title={shortDetailTitle} subtitle={detailSubtitle}>
      {#snippet actions()}
        {#if onArchiveState && canArchiveState(detail)}
          <button
            class="hero-action icon-hero-action"
            type="button"
            title="Archive CAR state without deleting git files"
            aria-label={`Archive CAR state for ${detail.title}`}
            onclick={(event) => handleArchiveClick(event, {
              kind: detail.kind,
              id: detail.id,
              label: shortDetailTitle,
              hasCarState: detail.hasCarState,
              unboundManagedThreadCount: detail.unboundManagedThreadCount
            })}
          >
            <span class="emoji-icon" aria-hidden="true">🧹</span>
            <span>Archive</span>
          </button>
        {/if}
        {#if detail.kind === 'worktree' && onCleanupWorktree}
          <button
            class="hero-action icon-hero-action danger"
            type="button"
            title="Cleanup worktree: archive a snapshot, then delete the checkout"
            aria-label={`Cleanup worktree ${detail.title}`}
            onclick={(event) => handleCleanupClick(event, {
              id: detail.id,
              label: shortDetailTitle,
              chatBound: detail.chatBound,
              cleanupBlockedByChatBinding: detail.cleanupBlockedByChatBinding
            })}
          >
            {@render trashIcon()}
            <span>Cleanup</span>
          </button>
        {/if}
      {/snippet}
    </PageHero>

    {#if detail.gitStatus}
      {@const git = detail.gitStatus}
      <div class="git-status-bar" aria-label="Git status">
        <span class={`git-state-pill ${git.dirty ? 'dirty' : 'clean'}`}>
          <span class="git-state-dot" aria-hidden="true"></span>
          {git.dirty ? 'Dirty' : 'Clean'}
        </span>
        {#if git.filesChanged !== null && git.filesChanged > 0}
          <span class="git-chip">{pluralize(git.filesChanged, 'file')} changed</span>
        {/if}
        <TicketDiffStats
          extraClass="git-chip git-chip-diff"
          stats={{
            insertions: git.insertions ?? 0,
            deletions: git.deletions ?? 0,
            filesChanged: 0
          }}
        />
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
            {#if onSyncRepo}
              <button
                type="button"
                class="git-sync-btn"
                disabled={syncRepoBusy || git.dirty}
                title={git.dirty
                  ? 'Commit or stash changes before syncing'
                  : 'Fetch and fast-forward the default branch from origin'}
                aria-busy={syncRepoBusy ? 'true' : undefined}
                onclick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  void onSyncRepo?.();
                }}
              >
                {syncRepoBusy ? 'Syncing…' : 'Sync'}
              </button>
            {/if}
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
              {#if detail.ticketOverview.total > 0}
                {@const ticketPct = Math.round((detail.ticketOverview.done / detail.ticketOverview.total) * 100)}
                <span
                  class="ticket-progress ticket-progress--detail-run"
                  title={`${detail.ticketOverview.done}/${detail.ticketOverview.total} tickets done (${ticketPct}%)`}
                  role="progressbar"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow={ticketPct}
                  aria-label={`Tickets ${detail.ticketOverview.done} of ${detail.ticketOverview.total} complete`}
                  style:--progress={`${ticketPct}%`}
                ></span>
              {:else if run.progress !== null}
                <span
                  class={`progress-track progress-track--detail-run status-${run.status}`}
                  aria-label={`${run.progress} percent complete`}
                  role="progressbar"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow={Math.round(run.progress)}
                >
                  <span style={`width: ${run.progress}%`}></span>
                </span>
              {/if}
              <div class="row-links">
                {#if run.chatHref}<a href={href(run.chatHref)}>Chat</a>{/if}
                {#if run.ticketHref}<a href={href(run.ticketHref)}>Ticket</a>{/if}
              </div>
            </article>
          {/each}
        </section>
      {/if}

      <section class="page-panel execution-panel wide contextspace-panel">
        <div class="panel-heading-row">
          <a
            class="panel-heading-primary-link"
            href={href(detail.contextspaceHref)}
            data-sveltekit-preload-data="tap"
          >
            <h2>Contextspace</h2>
            <span class="panel-heading-primary-hint">Browse all<span aria-hidden="true"> →</span></span>
          </a>
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
          <a
            class="panel-heading-primary-link"
            href={href(detail.ticketIndexHref)}
            data-sveltekit-preload-data="tap"
          >
            <h2>{detail.kind === 'worktree' ? 'Worktree tickets' : 'Repo tickets'}</h2>
            <span class="panel-heading-primary-hint">All tickets<span aria-hidden="true"> →</span></span>
          </a>
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
                  <TicketDiffStats stats={ticket.diffStats} />
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
            <a class="chip-button" href={href(detail.pmaChatHref)} data-sveltekit-preload-data="tap">+ Chat</a>
            <a class="chip-button" href={href(detail.codingAgentChatHref)} data-sveltekit-preload-data="tap">+ Coding agent</a>
          </div>
        </div>
        {#if detail.chatList.totalChatCount > 0}
          {@const accentHex = repoAccent(detail.title)}
          {@const initials = repoInitials(detail.title)}
          <div class="chat-row-list">
            {#each detail.chatList.groups as group (group.key)}
              <details class={`scoped-chat-run-group status-${group.status}`} open={group.waitingCount > 0 || group.activeCount > 0}>
                <summary class="scoped-chat-run-summary">
                  <span
                    class="chat-row-glyph repo-mini-glyph"
                    style={`--glyph-accent: ${accentHex}`}
                    aria-hidden="true"
                  >{initials}</span>
                  <span class="scoped-chat-run-main">
                    <span class="scoped-chat-run-title-row">
                      <strong>{group.scopeLabel}</strong>
                      <span class="chat-scope-kind-tag tickets">Tickets</span>
                      <span class="chat-run-count-chip"><strong>{group.totalCount}</strong> {group.totalCount === 1 ? 'chat' : 'chats'}</span>
                      {#if group.status !== 'idle' && group.status !== 'done'}
                        <span class={`status-pill ${group.status}`}>{statusLabel(group.status)}</span>
                      {/if}
                      <span class="scoped-chat-run-trailing">
                        {#if group.updatedAt}
                          <span class="updated-at">{rowRelativeTime({ updatedAt: group.updatedAt })}</span>
                        {/if}
                        <span class="scoped-chat-run-chevron" aria-hidden="true">▸</span>
                      </span>
                    </span>
                    <span class="scoped-chat-run-meta">
                      {#if group.waitingCount > 0}<span>{group.waitingCount} waiting</span><span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                      {#if group.activeCount > 0}<span>{group.activeCount} active</span><span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                      {#if group.failedCount > 0}<span>{group.failedCount} failed</span><span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                      <span>{group.doneCount}/{group.totalCount} done</span>
                      {#if group.agents.length > 0}
                        <span class="chat-meta-dot" aria-hidden="true">·</span>
                        <span class="chat-agent">{group.agents.join(', ')}</span>
                      {/if}
                    </span>
                  </span>
                </summary>
                <div class="scoped-chat-run-children">
                  <VirtualList
                    items={group.chats}
                    key={(chat) => chat.id}
                    estimatedItemSize={58}
                    overscan={6}
                    initialCount={32}
                    ariaLabel={`Chats in ${group.scopeLabel}`}
                    class="scoped-chat-run-child-list"
                  >
                  {#snippet children(chat)}
                    <a class={`scoped-chat-child-row status-${chat.status}`} href={href(chat.href)}>
                      <span class={`status-dot status-${chat.status}`} aria-hidden="true"></span>
                      <span class="scoped-chat-child-title">
                        <strong>{chat.ticketId ?? chat.title}</strong>
                        {#if chat.ticketId && chat.title && chat.title !== chat.ticketId}
                          <span class="scoped-chat-child-subtitle">{chat.title}</span>
                        {/if}
                      </span>
                      <span class="scoped-chat-child-meta">
                        {#if chat.agentId}<span class="chat-agent">{chat.agentId}</span>{/if}
                        {#if chat.agentId && chat.model}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                        {#if chat.model}<span class="chat-model">{chat.model}</span>{/if}
                        {#if chat.status !== 'idle' && chat.status !== 'done'}
                          <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
                        {/if}
                        {#if chat.updatedAt}<span class="updated-at">{rowRelativeTime({ updatedAt: chat.updatedAt })}</span>{/if}
                        <span class="scoped-chat-child-arrow" aria-hidden="true">→</span>
                      </span>
                    </a>
                  {/snippet}
                  </VirtualList>
                </div>
              </details>
            {/each}
            {#each detail.chatList.standaloneChats.slice(0, 5) as chat}
              {@const metaBits = [chat.agentId, chat.model].filter((p): p is string => typeof p === 'string' && p.length > 0)}
              <a class={`chat-row status-${chat.status}`} href={href(chat.href)}>
                <span
                  class="chat-row-glyph repo-mini-glyph"
                  style={`--glyph-accent: ${accentHex}`}
                  aria-hidden="true"
                >{initials}</span>
                <span class="chat-row-main">
                  <span class="chat-title-row">
                    <span class="chat-title-cluster">
                      <span class="chat-title-text-badge">
                        <strong>{chat.title}</strong>
                        <span class={`chat-scope-kind-tag ${detail.kind}`}>{detail.kind === 'repo' ? 'REPO' : 'WORKTREE'}</span>
                        <span class={`chat-kind-badge ${chat.kind}`}>{chat.kindLabel}</span>
                      </span>
                    </span>
                    <span class="chat-title-trailing">
                      {#if chat.status !== 'idle' && chat.status !== 'done'}
                        <span class={`status-pill ${chat.status}`}>{statusLabel(chat.status)}</span>
                      {/if}
                      {#if chat.updatedAt}
                        <span class="updated-at">{rowRelativeTime({ updatedAt: chat.updatedAt })}</span>
                      {/if}
                    </span>
                  </span>
                  {#if metaBits.length > 0}
                    <span class="chat-meta-row">
                      <span class="chat-agent-model">
                        {#each metaBits as bit, i}
                          {#if i > 0}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                          <span class={i === 0 ? 'chat-agent' : 'chat-model'}>{bit}</span>
                        {/each}
                      </span>
                    </span>
                  {/if}
                </span>
              </a>
            {/each}
            {#if detail.chatList.standaloneChats.length > 5}
              <a class="row-overflow-link" href={href('/chats')}>+{detail.chatList.standaloneChats.length - 5} more chat{detail.chatList.standaloneChats.length - 5 === 1 ? '' : 's'}</a>
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
    <VirtualList
      items={items}
      key={(item) => item.id}
      estimatedItemSize={58}
      overscan={6}
      initialCount={32}
      ariaLabel="Surfaced artifacts"
      class="activity-list compact-activity-list"
    >
      {#snippet children(item)}
        <a class="dashboard-row activity-row" href={href(item.href ?? '/chats')}>
          <span class={`activity-kind ${item.kind}`}>{item.kind}</span>
          <span>
            <span class="row-title">{item.title}</span>
            <span class="row-meta">{item.summary} · {rowRelativeTime({ createdAt: item.createdAt })}</span>
          </span>
        </a>
      {/snippet}
    </VirtualList>
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

{#snippet trashIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M3 6h18" />
    <path d="M8 6V4h8v2" />
    <path d="M6 6l1 15h10l1-15" />
    <path d="M10 10v7" />
    <path d="M14 10v7" />
  </svg>
{/snippet}

{#snippet chevronIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true" class="chevron-svg">
    <path d="M8 10l4 4 4-4" />
  </svg>
{/snippet}

{#snippet collapseAllIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M7 14l5-5 5 5" />
    <path d="M7 19l5-5 5 5" />
  </svg>
{/snippet}

{#snippet expandAllIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M7 5l5 5 5-5" />
    <path d="M7 10l5 5 5-5" />
  </svg>
{/snippet}

<style>
  .repos-index-v2 {
    gap: var(--space-3);
  }

  .repos-controls {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: 0 2px;
  }

  .repos-controls-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
  }

  .repos-search {
    flex: 1 1 auto;
    min-width: 0;
  }

  .row-action-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    min-height: 28px;
    padding: 4px 10px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-family: inherit;
    font-size: var(--font-size-0);
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: border-color var(--transition-fast), background-color var(--transition-fast), color var(--transition-fast);
  }
  .row-action-button:hover {
    border-color: color-mix(in srgb, var(--color-accent) 38%, var(--color-border));
    background: var(--color-accent-soft);
    color: var(--color-accent);
  }

  .repos-empty {
    border-radius: 12px;
    padding: var(--space-5);
  }

  .repos-list {
    --virtual-list-gap: var(--space-3);
    height: min(76vh, 1100px);
    margin: 0;
    padding: 0;
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

  .repo-head {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding-right: var(--space-5);
  }

  .repo-collapse-toggle {
    display: inline-grid;
    place-items: center;
    flex: 0 0 auto;
    width: 24px;
    height: 24px;
    margin-left: var(--space-3);
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    transition: background-color var(--transition-fast), color var(--transition-fast);
  }
  .repo-collapse-toggle:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }
  .repo-collapse-toggle:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--color-accent) 55%, transparent);
    outline-offset: 2px;
  }
  .repo-collapse-toggle .chevron-svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform var(--transition-base);
  }
  .repo-item.is-collapsed .repo-collapse-toggle .chevron-svg {
    transform: rotate(-90deg);
  }

  .collapse-all-chip {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .collapse-all-chip svg {
    width: 12px;
    height: 12px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .count-chip.is-in-use {
    background: color-mix(in srgb, var(--color-warning) 12%, transparent);
    color: var(--color-warning);
    border: 1px solid color-mix(in srgb, var(--color-warning) 28%, transparent);
  }
  .count-chip.is-in-use strong,
  .count-chip.is-in-use em {
    color: var(--color-warning);
  }
  .count-chip.is-in-use.idle {
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    border-color: var(--color-border-subtle);
  }
  .count-chip.is-in-use.idle strong,
  .count-chip.is-in-use.idle em {
    color: var(--color-ink-muted);
  }

  .repo-card {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-4) var(--space-5);
    color: var(--color-ink);
  }

  .repo-card-main {
    grid-column: 1 / 3;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: var(--space-4);
    min-width: 0;
    color: inherit;
    text-decoration: none;
    border-radius: 8px;
    margin: calc(var(--space-1) * -1);
    padding: var(--space-1);
  }

  .repo-card-main:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--color-accent) 55%, transparent);
    outline-offset: 2px;
  }

  .repo-head-actions {
    flex: 0 0 auto;
  }

  .repo-signal-pills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-5) var(--space-3);
    border-top: 1px solid var(--color-border-subtle);
    background: var(--color-surface-muted);
  }

  .repo-action-buttons {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 auto;
  }

  .emoji-icon {
    display: inline-grid;
    place-items: center;
    font-size: 14px;
    line-height: 1;
    filter: grayscale(0.2);
  }

  .icon-action {
    display: inline-grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 6px;
    border: 1px solid var(--color-border-subtle);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast), background-color var(--transition-fast);
  }

  .icon-action:hover {
    color: var(--color-ink);
    border-color: var(--color-border-strong);
    background: var(--color-surface-muted);
  }

  .icon-action.cleanup:hover,
  .icon-hero-action.danger:hover {
    color: var(--color-danger);
    border-color: color-mix(in srgb, var(--color-danger) 40%, var(--color-border));
  }

  .icon-action svg {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .icon-hero-action {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .icon-hero-action svg {
    width: 15px;
    height: 15px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
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

  .repo-card-counts {
    grid-column: 3;
    justify-self: end;
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

  .count-chip-progress {
    margin-left: 4px;
    padding-left: 6px;
    border-left: 1px solid color-mix(in srgb, currentColor 35%, transparent);
    font-variant-numeric: tabular-nums;
    opacity: 0.85;
    font-size: 10px;
  }

  .ticket-progress {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 2px;
    background: color-mix(in srgb, var(--color-accent) 14%, transparent);
    overflow: hidden;
    pointer-events: none;
  }

  .ticket-progress::after {
    content: "";
    position: absolute;
    inset: 0;
    width: var(--progress, 0%);
    background: var(--color-accent);
    transition: width var(--transition-base);
  }

  /* Match horizontal extent of nested `.worktree-list` rows: list uses the same
   * horizontal padding; `repo-head` already has matching `padding-right`. */
  .ticket-progress--repo-head {
    left: var(--space-5);
    right: 0;
    z-index: 1;
  }

  .ticket-progress--worktree {
    height: 2px;
    background: color-mix(in srgb, var(--color-accent) 14%, transparent);
  }

  /* Repo/worktree detail — active run: same done/total width as index `.ticket-progress`,
   * thicker rail + success fill (index keeps accent/teal). */
  .ticket-progress--detail-run {
    position: relative;
    left: auto;
    right: auto;
    bottom: auto;
    width: 100%;
    height: var(--space-2);
    border-radius: 999px;
    background: color-mix(in srgb, var(--color-success) 16%, transparent);
    pointer-events: none;
  }

  .ticket-progress--detail-run::after {
    background: var(--color-success);
  }

  a.count-chip-link {
    text-decoration: none;
    transition: filter var(--transition-base), box-shadow var(--transition-base);
  }

  a.count-chip-link:hover {
    filter: brightness(0.95);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent) 20%, transparent);
  }

  a.count-chip-link.is-active:hover {
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-success) 22%, transparent);
  }

  .repo-status {
    text-transform: lowercase;
  }

  /* Worktree children — Linear-style nested list with rail */
  .worktree-list {
    --virtual-list-gap: 0;
    max-height: min(520px, 54vh);
    margin: 0;
    padding: 0 var(--space-5) var(--space-3);
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
    grid-template-columns: 28px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) 0 var(--space-2) 8px;
    color: var(--color-ink);
    text-decoration: none;
    border-radius: 6px;
    transition: background-color var(--transition-fast);
  }

  .worktree-card > .repo-action-buttons {
    grid-column: 4;
    justify-self: end;
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

  /* Status accent strip removed in favor of the bottom progress bar. */

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
    .repo-card-main {
      grid-column: 1 / -1;
      grid-row: 1;
    }
    .repo-card-counts {
      grid-column: 1 / -1;
      grid-row: 2;
      justify-self: start;
    }
    .worktree-list {
      padding: 0 var(--space-4) var(--space-3);
    }

    .repo-head {
      padding-right: var(--space-4);
    }

    .ticket-progress--repo-head {
      left: var(--space-4);
      right: 0;
    }
    .worktree-card {
      grid-template-columns: 24px minmax(0, 1fr);
      grid-template-rows: auto auto;
      row-gap: 4px;
    }
    .worktree-card-counts {
      grid-column: 2;
    }
    .worktree-card > .repo-action-buttons {
      grid-column: 2;
      justify-self: start;
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
    border-radius: var(--radius-2);
    border: 1px solid var(--color-border-subtle);
    background: var(--color-surface);
    color: inherit;
    text-decoration: none;
    transition: border-color var(--transition-fast), background var(--transition-fast),
      color var(--transition-fast);
  }

  .contextspace-row:hover {
    border-color: var(--color-accent);
    background: var(--color-surface-muted);
    color: var(--color-accent);
  }
  .contextspace-row:hover .contextspace-row-title {
    color: var(--color-accent);
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
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-2);
    border: 1px solid var(--color-border-subtle);
    background: var(--color-surface);
    text-decoration: none;
    color: inherit;
    transition: border-color var(--transition-fast), background var(--transition-fast);
  }
  .chat-row:hover {
    border-color: var(--color-accent);
    background: var(--color-surface-muted);
  }
  .chat-row-main {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .row-overflow-link {
    display: block;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-2);
    border: 1px dashed var(--color-border-subtle);
    color: var(--color-ink-muted);
    text-decoration: none;
    font-size: var(--font-size-1);
    text-align: center;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }
  .row-overflow-link:hover {
    color: var(--color-accent);
    border-color: var(--color-accent);
  }

  .chip-button {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    min-height: 26px;
    padding: 0 var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-2);
    background: transparent;
    color: var(--color-ink-muted);
    font-family: var(--font-mono);
    font-size: var(--font-size-1);
    font-weight: 500;
    text-decoration: none;
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast),
      background var(--transition-fast);
  }
  .chip-button:hover {
    color: var(--color-accent);
    border-color: var(--color-accent);
    background: var(--color-accent-soft);
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

  .git-sync-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 22px;
    padding: 0 10px;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--color-accent) 35%, var(--color-border));
    background: var(--color-accent-soft);
    color: var(--color-accent);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: background-color var(--transition-fast), border-color var(--transition-fast), opacity var(--transition-fast);
    white-space: nowrap;
  }

  .git-sync-btn:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--color-accent) 55%, var(--color-border));
    background: color-mix(in srgb, var(--color-accent) 22%, transparent);
  }

  .git-sync-btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
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
    display: block;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-2);
    border: 1px dashed var(--color-border-subtle);
    background: transparent;
    font-size: var(--font-size-1);
    color: var(--color-ink-muted);
    text-decoration: none;
    text-align: center;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }

  .ticket-overview-more:hover {
    color: var(--color-accent);
    border-color: var(--color-accent);
  }

  .scoped-chat-run-group {
    border: 1px solid var(--color-border-subtle);
    border-radius: 10px;
    background: var(--color-surface);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }
  .scoped-chat-run-group:hover {
    border-color: var(--color-border-strong);
  }
  .scoped-chat-run-group + .scoped-chat-run-group,
  .scoped-chat-run-group + .chat-row,
  .chat-row + .scoped-chat-run-group {
    margin-top: var(--space-2);
  }
  .scoped-chat-run-summary {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    cursor: pointer;
    list-style: none;
  }
  .scoped-chat-run-summary::-webkit-details-marker { display: none; }
  .scoped-chat-run-group[open] > .scoped-chat-run-summary {
    border-bottom: 1px solid var(--color-border-subtle);
    background: var(--color-surface-muted);
  }
  .scoped-chat-run-group[open] .scoped-chat-run-chevron {
    transform: rotate(90deg);
  }
  .scoped-chat-run-chevron {
    color: var(--color-ink-faint);
    font-size: 11px;
    margin-left: var(--space-2);
    transition: transform var(--transition-base) var(--ease-out);
    display: inline-block;
  }
  .scoped-chat-run-main {
    flex: 1;
    min-width: 0;
    display: grid;
    gap: 4px;
  }
  .scoped-chat-run-title-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    font-size: var(--font-size-1);
    color: var(--color-ink);
    font-weight: 600;
  }
  .scoped-chat-run-trailing {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: 500;
    color: var(--color-ink-faint);
  }
  .scoped-chat-run-meta {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    font-size: var(--font-size-0);
    color: var(--color-ink-muted);
    font-variant-numeric: tabular-nums;
  }
  .scoped-chat-run-children {
    display: flex;
    flex-direction: column;
    background: var(--color-surface-sunken);
  }
  .scoped-chat-run-child-list {
    --virtual-list-gap: 0;
    max-height: min(420px, 48vh);
  }
  .scoped-chat-child-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3) var(--space-2) calc(var(--space-3) + 28px + var(--space-2));
    text-decoration: none;
    color: var(--color-ink);
    font-size: var(--font-size-1);
    transition: background-color var(--transition-fast);
    border-top: 1px solid var(--color-border-subtle);
  }
  .scoped-chat-child-row:first-child {
    border-top: 0;
  }
  .scoped-chat-child-row:hover {
    background: var(--color-surface-muted);
  }
  .scoped-chat-child-title {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    overflow: hidden;
  }
  .scoped-chat-child-title strong {
    font-weight: 600;
    font-family: var(--font-mono);
    font-size: var(--font-size-1);
    color: var(--color-ink);
  }
  .scoped-chat-child-subtitle {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .scoped-chat-child-meta {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--color-ink-faint);
    font-size: var(--font-size-0);
  }
  .scoped-chat-child-arrow {
    color: var(--color-ink-faint);
    transition: transform var(--transition-fast), color var(--transition-fast);
  }
  .scoped-chat-child-row:hover .scoped-chat-child-arrow {
    color: var(--color-ink-soft);
    transform: translateX(2px);
  }
</style>
