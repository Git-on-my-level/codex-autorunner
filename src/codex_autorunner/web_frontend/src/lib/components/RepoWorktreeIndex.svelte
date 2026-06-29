<script lang="ts">
  import { goto } from '$app/navigation';
  import type { RepoWorktreeIndexFilter, RepoWorktreeIndexViewModel } from '$lib/viewModels/repoWorktree';
  import {
    countRepoWorktreeIndexEntities,
    filterRepoWorktreeIndexRows,
    rowRelativeTime,
    visibleRepoWorktreeChildren,
    isStaleWorktreeChild,
    repoDefaultExpanded,
    DEFAULT_VISIBLE_CHILD_CAP
  } from '$lib/viewModels/repoWorktree';
  import { loadRepoIndexPrefs, saveRepoIndexPrefs } from '$lib/viewModels/repoIndexPrefs';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/chat';
  import type { PartialPageIssue } from '$lib/api/client';
  import TicketDiffStats from '$lib/components/tickets/TicketDiffStats.svelte';
  import VirtualList from '$lib/components/VirtualList.svelte';
  import FilterRow from '$lib/components/FilterRow.svelte';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import './RepoWorktreeViews.css';

  let {
    index,
    sectionIssues = [],
    onRetry = undefined,
    onArchiveWorktree = undefined,
    onRetireWorktree = undefined,
    onRetireState = undefined,
    onRepoPin = undefined,
    onCreateRepo = undefined,
    onCreateWorktree = undefined,
    onOpenRepoSettings = undefined
  }: {
    index: RepoWorktreeIndexViewModel;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
    onArchiveWorktree?: ((worktree: { id: string; label: string; archived: boolean }) => void | Promise<void>) | undefined;
    onRetireWorktree?: ((worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }) => void | Promise<void>) | undefined;
    onRetireState?: ((target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }) => void | Promise<void>) | undefined;
    onRepoPin?: ((target: { id: string; pinned: boolean }) => void | Promise<void>) | undefined;
    onCreateRepo?: (() => void) | undefined;
    onCreateWorktree?: ((target: { id: string; label: string }) => void) | undefined;
    onOpenRepoSettings?: ((target: { id: string; label: string; worktreeSetupCommands: string[] }) => void) | undefined;
  } = $props();

  const currentRunIssues = $derived(sectionIssues.filter((issue) => issue.id === 'current_run'));
  const ticketIssues = $derived(sectionIssues.filter((issue) => issue.id === 'tickets'));

  const REPO_FILTERS: RepoWorktreeIndexFilter[] = ['all', 'waiting', 'active', 'chat_bound', 'archived'];
  let search = $state('');
  let filter = $state<RepoWorktreeIndexFilter>('all');

  type IndexRow = RepoWorktreeIndexViewModel['rows'][number];

  const initialPrefs = loadRepoIndexPrefs();
  // Explicit per-repo collapse overrides (true = collapsed). Absent = use repoDefaultExpanded.
  let collapsedOverrides = $state<Record<string, boolean>>(initialPrefs.collapsed);
  let hideStale = $state(initialPrefs.hideStale);
  // Per-repo "show all worktrees" disclosure; transient, not persisted.
  let expandedChildRepoIds = $state<Record<string, true>>({});

  $effect(() => {
    saveRepoIndexPrefs({ collapsed: collapsedOverrides, hideStale });
  });

  const searching = $derived(search.trim().length > 0);

  function isRepoCollapsed(row: IndexRow): boolean {
    const override = collapsedOverrides[row.id];
    if (typeof override === 'boolean') return override;
    return !repoDefaultExpanded(row);
  }

  function toggleRepoCollapsed(row: IndexRow): void {
    collapsedOverrides = { ...collapsedOverrides, [row.id]: !isRepoCollapsed(row) };
  }

  const indexRows = $derived(index.rows);
  const ticketMetricsReady = $derived(index.ticketIndexMetricsAvailable);
  const filteredRows = $derived(filterRepoWorktreeIndexRows(indexRows, search, filter));
  const collapsibleRepos = $derived(
    filteredRows.filter((row) => row.kind === 'repo' && row.totalWorktrees > 0)
  );
  const collapsibleRepoCount = $derived(collapsibleRepos.length);
  const allCollapsed = $derived(
    collapsibleRepos.length > 0 && collapsibleRepos.every((row) => isRepoCollapsed(row))
  );
  const hasCollapsedDefaultExpanded = $derived(
    collapsibleRepos.some((row) => repoDefaultExpanded(row) && isRepoCollapsed(row))
  );
  const staleCount = $derived(
    indexRows.reduce(
      (total, row) => total + visibleRepoWorktreeChildren(row, '', 'all').filter((c) => isStaleWorktreeChild(c)).length,
      0
    )
  );

  function setAllCollapsed(value: boolean): void {
    const next = { ...collapsedOverrides };
    for (const row of collapsibleRepos) next[row.id] = value;
    collapsedOverrides = next;
  }

  function expandActive(): void {
    const next = { ...collapsedOverrides };
    for (const row of collapsibleRepos) {
      if (repoDefaultExpanded(row)) next[row.id] = false;
    }
    collapsedOverrides = next;
  }

  function visibleChildren(row: IndexRow) {
    const rows = visibleRepoWorktreeChildren(row, search, filter);
    // Keep matches visible while searching; hide-stale only applies to the resting list.
    if (searching || !hideStale) return rows;
    return rows.filter((child) => !isStaleWorktreeChild(child));
  }

  function toggleShowAllChildren(repoId: string): void {
    if (expandedChildRepoIds[repoId]) {
      const next = { ...expandedChildRepoIds };
      delete next[repoId];
      expandedChildRepoIds = next;
    } else {
      expandedChildRepoIds = { ...expandedChildRepoIds, [repoId]: true };
    }
  }

  function repoFilterCount(key: RepoWorktreeIndexFilter): number {
    if (key === 'all') return countRepoWorktreeIndexEntities(indexRows);
    if (key === 'active') return index.activeCount;
    if (key === 'chat_bound') return index.chatBoundCount;
    if (key === 'archived') return index.archivedCount;
    return index.waitingCount;
  }

  function repoFilterLabel(key: RepoWorktreeIndexFilter): string {
    if (key === 'chat_bound') return 'Chat-bound';
    if (key === 'archived') return 'Archived';
    return key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1);
  }

  function chatBindingLabel(target: { chatBindingDisplayNames: string[]; chatBindingSources: Record<string, number>; chatBindingCount: number }): string {
    if (target.chatBindingDisplayNames.length > 0) return target.chatBindingDisplayNames.join(', ');
    const sources = Object.entries(target.chatBindingSources)
      .filter(([, count]) => count > 0)
      .map(([source, count]) => `${source}${count > 1 ? ` ${count}` : ''}`);
    if (sources.length > 0) return sources.join(', ');
    return target.chatBindingCount > 1 ? `${target.chatBindingCount} chats` : 'Chat-bound';
  }

  function canRetireState(target: { hasCarState: boolean; unboundManagedThreadCount: number }): boolean {
    return target.hasCarState || target.unboundManagedThreadCount > 0;
  }

  function handleRetireClick(
    event: MouseEvent,
    worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onRetireWorktree?.(worktree);
  }

  function handleArchiveClick(
    event: MouseEvent,
    worktree: { id: string; label: string; archiveState: 'active' | 'archived' }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onArchiveWorktree?.({ id: worktree.id, label: worktree.label, archived: worktree.archiveState === 'archived' });
  }

  function handleRetireStateClick(
    event: MouseEvent,
    target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onRetireState?.(target);
  }

  function handleRepoPinClick(event: MouseEvent, id: string, pinned: boolean): void {
    event.preventDefault();
    event.stopPropagation();
    void onRepoPin?.({ id, pinned });
  }

  function isInteractiveTarget(target: EventTarget | null): boolean {
    return target instanceof Element && Boolean(target.closest('a, button, input, select, textarea, [role="button"]'));
  }

  function openRow(event: MouseEvent | KeyboardEvent, path: string): void {
    if (isInteractiveTarget(event.target)) return;
    event.preventDefault();
    void goto(href(path));
  }
</script>

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
        <FilterRow
          ariaLabel="Repo status filters"
          items={REPO_FILTERS.map((item) => {
            const count = repoFilterCount(item);
            return {
              key: item,
              label: repoFilterLabel(item),
              count,
              active: filter === item,
              className: count === 0 ? 'filter-chip-zero' : '',
              onSelect: () => (filter = item)
            };
          })}
        >
          {#snippet trailing()}
            {#if staleCount > 0 || hideStale}
              <button
                class="ghost-button hide-stale-button"
                class:is-on={hideStale}
                type="button"
                title={hideStale ? 'Show stale worktrees' : 'Hide stale worktrees (untouched 14+ days)'}
                aria-pressed={hideStale}
                onclick={() => (hideStale = !hideStale)}
              >
                {hideStale ? `Show stale${staleCount > 0 ? ` (${staleCount})` : ''}` : `Hide stale (${staleCount})`}
              </button>
            {/if}
            {#if hasCollapsedDefaultExpanded}
              <button
                class="ghost-button"
                type="button"
                title="Expand repos with active or pinned work"
                onclick={() => expandActive()}
              >
                Expand active
              </button>
            {/if}
            {#if collapsibleRepoCount > 0}
              <button
                class="ghost-button collapse-all-button"
                type="button"
                title={allCollapsed ? 'Expand all repos' : 'Collapse all repos'}
                aria-label={allCollapsed ? 'Expand all repos' : 'Collapse all repos'}
                onclick={() => setAllCollapsed(!allCollapsed)}
              >
                {#if allCollapsed}
                  {@render expandAllIcon()}
                  <span>Expand all</span>
                {:else}
                  {@render collapseAllIcon()}
                  <span>Collapse all</span>
                {/if}
              </button>
            {/if}
          {/snippet}
        </FilterRow>
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
        key={(row) => `${row.id}:${row.isPinned ? 1 : 0}`}
        estimatedItemSize={122}
        overscan={8}
        initialCount={40}
        ariaLabel="Repo and worktree index"
        class="repos-list"
      >
        {#snippet children(row)}
          {@const accent = repoAccent(row.label)}
          {@const collapsible = row.kind === 'repo' && row.totalWorktrees > 0}
          {@const collapsed = collapsible && isRepoCollapsed(row)}
          <div class={`repo-item status-${row.status}`} class:has-children={row.childWorktrees.length > 0} class:is-collapsed={collapsed} class:is-archived={row.archiveState === 'archived'} role="listitem" style={`--repo-accent: ${accent};`}>
            <div
              class="repo-head row-click-target"
              role="link"
              tabindex="0"
              aria-label={`Open ${row.label} detail`}
              onclick={(event) => openRow(event, row.href)}
              onkeydown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') openRow(event, row.href);
              }}
            >
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
                  toggleRepoCollapsed(row);
                }}
              >
                {@render chevronIcon()}
              </button>
            {:else if row.kind === 'repo'}
              <span class="repo-collapse-toggle is-placeholder" aria-hidden="true"></span>
            {/if}
            <div class="repo-card">
              {#if row.kind === 'repo' && onRepoPin}
                <button
                  class="repo-avatar-slot pinnable"
                  class:is-pinned={row.isPinned}
                  type="button"
                  title={row.isPinned ? 'Unpin repo' : 'Pin repo'}
                  aria-label={row.isPinned ? `Unpin ${row.label}` : `Pin ${row.label}`}
                  aria-pressed={row.isPinned}
                  onclick={(event) => handleRepoPinClick(event, row.id, !row.isPinned)}
                >
                  <span class="repo-avatar" aria-hidden="true">{repoInitials(row.label)}</span>
                  <span class="repo-pin-glyph" aria-hidden="true">📌</span>
                </button>
              {:else}
                <span class="repo-avatar-slot">
                  <span class="repo-avatar" aria-hidden="true">{repoInitials(row.label)}</span>
                </span>
              {/if}
              <a class="repo-card-main" href={href(row.href)} aria-label={`Open ${row.label} detail`}>
                <div class="repo-card-body">
                  <div class="repo-card-title">
                    <span class="repo-name">{row.label}</span>
                    {#if row.status !== 'idle' && row.status !== 'done'}
                      <span class={`repo-status status-pill ${row.status}`}>{statusLabel(row.status)}</span>
                    {/if}
                    {#if row.signalWaiting > 0}
                      <span class="response-needed-pill" title="A paused dispatch needs your response">
                        Respond
                      </span>
                    {/if}
                    {#if row.chatBound}
                      <span class="chat-binding-pill" title={chatBindingLabel(row)}>
                        Chat-bound
                      </span>
                    {/if}
                    {#if row.archiveState === 'archived'}
                      <span class="archive-pill">Archived</span>
                    {/if}
                  </div>
                  <div class="repo-card-meta">
                    {#if row.branch}
                      <span class="repo-meta-branch"><span class="branch-glyph" aria-hidden="true">⎇</span>{row.branch}</span>
                    {/if}
                    {#if row.detail}
                      {#if row.branch}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                      <span>{row.detail}</span>
                    {/if}
                    {#if row.chatBound}
                      {#if row.branch || row.detail}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                      <span class="chat-binding-meta">{chatBindingLabel(row)}</span>
                    {/if}
                    {#if row.lastActivityAt}
                      {#if row.branch || row.detail || row.chatBound}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                      <span class="repo-meta-time">{rowRelativeTime(row)}</span>
                    {/if}
                  </div>
                </div>
              </a>
              {#if row.activeRuns > 0 || (ticketMetricsReady && row.totalTickets > 0) || (collapsed && row.totalWorktrees > 0)}
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
                    <span
                      class="count-chip is-active"
                      title="Active runs"
                    >
                      <strong>{row.activeRuns}</strong><em>run{row.activeRuns === 1 ? '' : 's'}</em>
                    </span>
                  {/if}
                  {#if ticketMetricsReady && row.totalTickets > 0}
                    {@render ticketFlowChip(row)}
                  {/if}
                </div>
              {/if}
            </div>
            <div class="repo-action-buttons repo-head-actions" aria-label={`Actions for ${row.label}`}>
              <a
                class="row-action-button is-primary-affordance"
                href={href(row.chatHref)}
                title={`New chat scoped to ${row.label}`}
                aria-label={`New chat for ${row.label}`}
                data-sveltekit-preload-data="tap"
                onclick={(event) => event.stopPropagation()}
              >+ Chat</a>
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
                >+ Worktree</button>
              {/if}
              {#if
                (row.kind === 'repo' && onOpenRepoSettings) ||
                (row.kind === 'worktree' && onArchiveWorktree) ||
                (onRetireState && canRetireState(row)) ||
                (row.kind === 'worktree' && onRetireWorktree)}
                <span class="repo-head-icon-actions">
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
                      {@render settingsIcon()}
                    </button>
                  {/if}
                  {#if row.kind === 'worktree' && onArchiveWorktree}
                    <button
                      class="ghost-button archive-action"
                      type="button"
                      title={row.archiveState === 'archived' ? 'Unarchive worktree' : 'Archive worktree without deleting checkout'}
                      aria-label={row.archiveState === 'archived' ? `Unarchive worktree ${row.label}` : `Archive worktree ${row.label}`}
                      onclick={(event) => handleArchiveClick(event, row)}
                    >
                      {row.archiveState === 'archived' ? 'Unarchive' : 'Archive'}
                    </button>
                  {/if}
                  {#if onRetireState && canRetireState(row)}
                    <button
                      class="icon-action retire-state"
                      type="button"
                      title="Retire CAR state without deleting git files"
                      aria-label={`Retire CAR state for ${row.label}`}
                      onclick={(event) => handleRetireStateClick(event, {
                        kind: row.kind,
                        id: row.id,
                        label: row.label,
                        hasCarState: row.hasCarState,
                        unboundManagedThreadCount: row.unboundManagedThreadCount
                      })}
                    >
                      {@render clearStateIcon()}
                    </button>
                  {/if}
                  {#if row.kind === 'worktree' && onRetireWorktree}
                    <button
                      class="icon-action retire"
                      type="button"
                      title="Retire worktree: preserve artifacts, then remove the checkout"
                      aria-label={`Retire worktree ${row.label}`}
                      onclick={(event) => handleRetireClick(event, row)}
                    >
                      {@render trashIcon()}
                    </button>
                  {/if}
                </span>
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

            {#if !collapsed}
              {@const allChildRows = visibleChildren(row)}
              {@const showAllChildren = searching || expandedChildRepoIds[row.id] === true}
              {@const childRows = showAllChildren ? allChildRows : allChildRows.slice(0, DEFAULT_VISIBLE_CHILD_CAP)}
              {@const hiddenChildCount = allChildRows.length - childRows.length}
              {#if childRows.length > 0}
                <VirtualList
                  items={childRows}
                  key={(worktree) => worktree.id}
                  estimatedItemSize={54}
                  overscan={6}
                  initialCount={32}
                  ariaLabel={`Worktrees owned by ${row.label}`}
                  class="worktree-list"
                  scrollable={false}
                >
                  {#snippet children(worktree)}
                    <div class={`worktree-item status-${worktree.status}`} class:is-archived={worktree.archiveState === 'archived'} class:is-stale={!searching && isStaleWorktreeChild(worktree)} role="listitem">
                    <div
                      class="worktree-card row-click-target"
                      role="link"
                      tabindex="0"
                      aria-label={`Open ${worktree.label} detail`}
                      onclick={(event) => openRow(event, worktree.href)}
                      onkeydown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') openRow(event, worktree.href);
                      }}
                    >
                      <span class="worktree-rail" aria-hidden="true"></span>
                      <span class="worktree-dot" aria-hidden="true"></span>
                      <div class="worktree-card-body">
                        <div class="worktree-card-title">
                          <a class="worktree-name" href={href(worktree.href)}>{worktree.label}</a>
                          {#if worktree.status !== 'idle' && worktree.status !== 'done'}
                            <span class={`status-pill ${worktree.status}`}>{statusLabel(worktree.status)}</span>
                          {/if}
                          {#if worktree.signalWaiting > 0}
                            <span class="response-needed-pill" title="A paused dispatch needs your response">
                              Respond
                            </span>
                          {/if}
                          {#if worktree.chatBound}
                            <span class="chat-binding-pill" title={chatBindingLabel(worktree)}>
                              Chat-bound
                            </span>
                          {/if}
                          {#if worktree.archiveState === 'archived'}
                            <span class="archive-pill">Archived</span>
                          {/if}
                        </div>
                        {#if (worktree.branch && worktree.branch !== worktree.label) || worktree.currentRunTitle || worktree.chatBound}
                          <div class="worktree-card-meta">
                            {#if worktree.branch && worktree.branch !== worktree.label}
                              <span class="repo-meta-branch"><span class="branch-glyph" aria-hidden="true">⎇</span>{worktree.branch}</span>
                            {/if}
                            {#if worktree.currentRunTitle}
                              {#if worktree.branch && worktree.branch !== worktree.label}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                              <span class="worktree-run-title">{worktree.currentRunTitle}</span>
                            {/if}
                            {#if worktree.chatBound}
                              {#if (worktree.branch && worktree.branch !== worktree.label) || worktree.currentRunTitle}<span class="repo-meta-dot" aria-hidden="true">·</span>{/if}
                              <span class="chat-binding-meta">{chatBindingLabel(worktree)}</span>
                            {/if}
                          </div>
                        {/if}
                      </div>
                      {#if worktree.activeRuns > 0 || (ticketMetricsReady && worktree.totalTickets > 0) || worktree.signalWaiting > 0 || worktree.signalFailed > 0 || worktree.signalActive > 0}
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
                          {#if ticketMetricsReady && worktree.totalTickets > 0}
                            {@render ticketFlowChip(worktree)}
                          {/if}
                        </div>
                      {/if}
                      <div class="repo-action-buttons" aria-label={`Actions for ${worktree.label}`}>
                        <a
                          class="row-action-button is-primary-affordance"
                          href={href(worktree.chatHref)}
                          title={`New chat scoped to ${worktree.label}`}
                          aria-label={`New chat for ${worktree.label}`}
                          data-sveltekit-preload-data="tap"
                          onclick={(event) => event.stopPropagation()}
                        >+ Chat</a>
                        {#if onArchiveWorktree || (onRetireState && canRetireState(worktree)) || onRetireWorktree}
                          <span class="worktree-row-icon-actions">
                            {#if onArchiveWorktree}
                              <button
                                class="ghost-button archive-action"
                                type="button"
                                title={worktree.archiveState === 'archived' ? 'Unarchive worktree' : 'Archive worktree without deleting checkout'}
                                aria-label={worktree.archiveState === 'archived' ? `Unarchive worktree ${worktree.label}` : `Archive worktree ${worktree.label}`}
                                onclick={(event) => handleArchiveClick(event, worktree)}
                              >
                                {worktree.archiveState === 'archived' ? 'Unarchive' : 'Archive'}
                              </button>
                            {/if}
                            {#if onRetireState && canRetireState(worktree)}
                              <button
                                class="icon-action retire-state"
                                type="button"
                                title="Retire CAR state without deleting git files"
                                aria-label={`Retire CAR state for ${worktree.label}`}
                                onclick={(event) => handleRetireStateClick(event, {
                                  kind: 'worktree',
                                  id: worktree.id,
                                  label: worktree.label,
                                  hasCarState: worktree.hasCarState,
                                  unboundManagedThreadCount: worktree.unboundManagedThreadCount
                                })}
                              >
                                {@render clearStateIcon()}
                              </button>
                            {/if}
                            {#if onRetireWorktree}
                              <button
                                class="icon-action retire"
                                type="button"
                                title="Retire worktree: preserve artifacts, then remove the checkout"
                                aria-label={`Retire worktree ${worktree.label}`}
                                onclick={(event) => handleRetireClick(event, worktree)}
                              >
                                {@render trashIcon()}
                              </button>
                            {/if}
                          </span>
                        {/if}
                      </div>
                    </div>
                    {#if worktree.chats.length > 0}
                      <ul class="worktree-chats-inline" aria-label={`Recent chats in ${worktree.label}`}>
                        {#each worktree.chats as chat (chat.id)}
                          <li>
                            <a class={`worktree-chat-link status-${chat.status}`} href={href(chat.href)}>
                              <span class={`status-dot status-${chat.status}`} aria-hidden="true"></span>
                              <span class="worktree-chat-title">{chat.title}</span>
                              {#if chat.ticketDone !== null}
                                <span class="worktree-chat-ticket-pill" title={chat.ticketDone ? 'Ticket done' : 'Ticket open'}>
                                  {chat.ticketDone ? 'done' : 'open'}
                                </span>
                              {/if}
                              {#if chat.updatedAt}
                                <span class="worktree-chat-time">{rowRelativeTime({ updatedAt: chat.updatedAt })}</span>
                              {/if}
                            </a>
                          </li>
                        {/each}
                      </ul>
                    {/if}
                    </div>
                  {/snippet}
                </VirtualList>
              {/if}
              {#if !searching && allChildRows.length > DEFAULT_VISIBLE_CHILD_CAP}
                <button
                  class="worktree-show-more"
                  type="button"
                  onclick={() => toggleShowAllChildren(row.id)}
                  aria-expanded={showAllChildren}
                >
                  {showAllChildren
                    ? 'Show fewer worktrees'
                    : `Show ${hiddenChildCount} more worktree${hiddenChildCount === 1 ? '' : 's'}`}
                </button>
              {/if}
            {/if}
          </div>
        {/snippet}
      </VirtualList>
    {/if}
  </section>

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

{#snippet ticketFlowChip(target: {
  ticketHref: string | null;
  totalTickets: number;
  doneTickets: number;
  activeTickets: number;
  failedTickets: number;
  queuedTickets: number;
})}
  {@const pct = target.totalTickets > 0 ? Math.round((target.doneTickets / target.totalTickets) * 100) : 0}
  {@const flowTitle = `${target.doneTickets} done · ${target.activeTickets} active · ${target.failedTickets} need fix · ${target.queuedTickets} queued`}
  {#if target.ticketHref}
    <a
      class="count-chip is-tickets count-chip-navigable ticket-flow-chip"
      class:has-failed={target.failedTickets > 0}
      class:has-active={target.activeTickets > 0}
      href={href(target.ticketHref)}
      title={flowTitle}
      style:--progress={`${pct}%`}
    >
      <span class="ticket-flow-chip-fill" aria-hidden="true"></span>
      <span class="ticket-flow-chip-text">
        <span class="ticket-flow-chip-count"><strong>{target.doneTickets}</strong><em>/{target.totalTickets}</em></span>
        <em>ticket{target.totalTickets === 1 ? '' : 's'}</em>
        {#if target.failedTickets > 0}
          <span class="ticket-flow-chip-dot ticket-flow-chip-dot--failed" aria-hidden="true"></span>
        {/if}
        {#if target.activeTickets > 0}
          <span class="ticket-flow-chip-dot ticket-flow-chip-dot--active" aria-hidden="true"></span>
        {/if}
      </span>
    </a>
  {:else}
    <span
      class="count-chip is-tickets ticket-flow-chip"
      class:has-failed={target.failedTickets > 0}
      class:has-active={target.activeTickets > 0}
      title={flowTitle}
      style:--progress={`${pct}%`}
    >
      <span class="ticket-flow-chip-fill" aria-hidden="true"></span>
      <span class="ticket-flow-chip-text">
        <span class="ticket-flow-chip-count"><strong>{target.doneTickets}</strong><em>/{target.totalTickets}</em></span>
        <em>ticket{target.totalTickets === 1 ? '' : 's'}</em>
        {#if target.failedTickets > 0}
          <span class="ticket-flow-chip-dot ticket-flow-chip-dot--failed" aria-hidden="true"></span>
        {/if}
        {#if target.activeTickets > 0}
          <span class="ticket-flow-chip-dot ticket-flow-chip-dot--active" aria-hidden="true"></span>
        {/if}
      </span>
    </span>
  {/if}
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

{#snippet settingsIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9c.27.62.88 1.03 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z" />
  </svg>
{/snippet}

{#snippet clearStateIcon()}
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M3 21h7" />
    <path d="M19 5l-2-2-9 9-2 5 5-2 9-9z" />
    <path d="M14 6l4 4" />
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
