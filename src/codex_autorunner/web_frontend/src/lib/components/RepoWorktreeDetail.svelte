<script lang="ts">
  import type { RepoWorktreeDetailViewModel } from '$lib/viewModels/repoWorktree';
  import { rowRelativeTime } from '$lib/viewModels/repoWorktree';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/chat';
  import type { PartialPageIssue } from '$lib/api/client';
  import PageHero from './PageHero.svelte';
  import TicketDiffStats from '$lib/components/tickets/TicketDiffStats.svelte';
  import VirtualList from '$lib/components/VirtualList.svelte';
  import { repoAccent, repoInitials } from '$lib/viewModels/repoIdentity';
  import './RepoWorktreeViews.css';

  let {
    detail,
    sectionIssues = [],
    onRetry = undefined,
    onRetireWorktree = undefined,
    onRetireState = undefined,
    onSyncRepo = undefined,
    syncRepoBusy = false
  }: {
    detail: RepoWorktreeDetailViewModel;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
    onRetireWorktree?: ((worktree: { id: string; label: string; chatBound: boolean; cleanupBlockedByChatBinding: boolean }) => void | Promise<void>) | undefined;
    onRetireState?: ((target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }) => void | Promise<void>) | undefined;
    onSyncRepo?: (() => void | Promise<void>) | undefined;
    syncRepoBusy?: boolean;
  } = $props();

  const currentRunIssues = $derived(sectionIssues.filter((issue) => issue.id === 'current_run'));
  const ticketIssues = $derived(sectionIssues.filter((issue) => issue.id === 'tickets'));
  const contextspaceIssues = $derived(sectionIssues.filter((issue) => issue.id === 'contextspace'));
  const artifactIssues = $derived(sectionIssues.filter((issue) => issue.id === 'artifacts'));

  function pluralize(count: number, singular: string, plural?: string): string {
    return `${count} ${count === 1 ? singular : plural ?? `${singular}s`}`;
  }

  const shortDetailTitle = $derived.by(() => {
    if (detail.kind === 'worktree' && detail.baseRepoLabel) {
      const prefix = `${detail.baseRepoLabel}--`;
      if (detail.title.startsWith(prefix)) return detail.title.slice(prefix.length);
    }
    return detail.title;
  });

  const detailSubtitle = $derived.by(() => {
    const parts: string[] = [];
    if (detail.branch) parts.push(detail.branch);
    if (detail.path) parts.push(detail.path);
    return parts.join(' · ');
  });

  const contextspaceHasContent = $derived(detail.contextspace.some((doc) => doc.status === 'present'));

  const ticketKanban = $derived.by(() => {
    const overview = detail.ticketOverview;
    const failed = overview.failed;
    const active = Math.max(0, overview.active - failed);
    const queued = Math.max(0, overview.open - overview.active - failed);
    return { queued, active, failed, done: overview.done, total: overview.total };
  });

  const showFlowStrip = $derived.by(() => {
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

  function handleRetireStateClick(
    event: MouseEvent,
    target: { kind: 'repo' | 'worktree'; id: string; label: string; hasCarState: boolean; unboundManagedThreadCount: number }
  ): void {
    event.preventDefault();
    event.stopPropagation();
    void onRetireState?.(target);
  }
</script>

<section class="page-stack repo-worktree-page">
    {#if detail.isMissing}
      <PageHero
        title={detail.title}
        subtitle={`Route id ${detail.id} does not match a known ${detail.kind} in the current hub inventory.`}
      >
        {#snippet actions()}
          <a class="ghost-button" href={href(detail.missingIndexHref)}>{detail.missingIndexLabel}</a>
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
        <a class="ghost-button is-primary" href={href(detail.chatHref)} data-sveltekit-preload-data="tap">+ New chat</a>
        <a class="ghost-button" href={href(detail.newTicketHref)} data-sveltekit-preload-data="tap">+ New ticket</a>
      {/snippet}
    </PageHero>

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

      {@render chatsSection()}

      <section class="page-panel execution-panel wide workspace-ticket-queue-panel">
        <div class="panel-heading-row">
          <h2>{detail.kind === 'worktree' ? 'Worktree tickets' : 'Repo tickets'}</h2>
          <div class="panel-heading-actions">
            <a
              class="ghost-button is-primary"
              href={href(detail.newTicketHref)}
              data-sveltekit-preload-data="tap"
            >+ New ticket</a>
            <a
              class="ghost-button"
              href={href(detail.ticketIndexHref)}
              data-sveltekit-preload-data="tap"
            >All tickets</a>
          </div>
        </div>
        {@render degradedIssues(ticketIssues)}
        {#if detail.ticketOverview.total > 0}
          <a
            class="ticket-kanban-strip"
            href={href(detail.ticketIndexHref)}
            aria-label="Ticket flow — view all tickets"
            data-sveltekit-preload-data="tap"
          >
            <div class="kanban-col kanban-queued"><span>Queued</span><strong>{ticketKanban.queued}</strong></div>
            <div class="kanban-col kanban-active"><span>Active</span><strong>{ticketKanban.active}</strong></div>
            <div class="kanban-col kanban-failed"><span>Needs fix</span><strong>{ticketKanban.failed}</strong></div>
            <div class="kanban-col kanban-done"><span>Done</span><strong>{ticketKanban.done}<em>/{ticketKanban.total}</em></strong></div>
          </a>
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
          {:else if ticketIssues.length === 0 && detail.ticketOverview.total === 0}
            <div class="panel-empty-card" role="status">
              <p class="panel-empty-card-title">No tickets yet for this {detail.kind}.</p>
              <div class="panel-empty-card-actions">
                <a class="ghost-button is-primary" href={href(detail.newTicketHref)} data-sveltekit-preload-data="tap">+ New ticket</a>
              </div>
            </div>
          {/if}
        </div>
      </section>

      <!-- chatsSection snippet is rendered above via {@render chatsSection()} -->
      {#snippet chatsSection()}
      <section class="page-panel execution-panel wide">
        <div class="panel-heading-row chats-panel-heading">
          <h2 class="panel-heading-link-host">
            <a class="panel-heading-link" href={href(detail.scopedChatListHref)} data-sveltekit-preload-data="tap">
              Chats<span class="panel-heading-link-chevron" aria-hidden="true">→</span>
            </a>
          </h2>
          <div class="panel-heading-actions">
            <a class="ghost-button" href={href(detail.chatHref)} data-sveltekit-preload-data="tap">New chat</a>
            <a class="ghost-button" href={href(detail.codingAgentChatHref)} data-sveltekit-preload-data="tap">New coding agent chat</a>
          </div>
        </div>
        {#if detail.chatList.totalChatCount === 0}
          <div class="panel-empty-card" role="status">
            <p class="panel-empty-card-title">No chats scoped to this {detail.kind} yet.</p>
            <div class="panel-empty-card-actions">
              <a class="ghost-button is-primary" href={href(detail.chatHref)} data-sveltekit-preload-data="tap">+ New chat</a>
              <a class="ghost-button" href={href(detail.codingAgentChatHref)} data-sveltekit-preload-data="tap">+ New coding agent chat</a>
            </div>
          </div>
        {/if}
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
                    estimatedItemSize={34}
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
                        <span class="chat-id-tag">#{chat.shortId}</span>
                        {#if chat.agentId}<span class="chat-meta-dot" aria-hidden="true">·</span><span class="chat-agent">{chat.agentId}</span>{/if}
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
            {#each detail.chatList.standaloneChats as chat}
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
                  <span class="chat-meta-row">
                    <span class="chat-id-tag">#{chat.shortId}</span>
                    {#if metaBits.length > 0}
                      <span class="chat-meta-dot" aria-hidden="true">·</span>
                      <span class="chat-agent-model">
                        {#each metaBits as bit, i}
                          {#if i > 0}<span class="chat-meta-dot" aria-hidden="true">·</span>{/if}
                          <span class={i === 0 ? 'chat-agent' : 'chat-model'}>{bit}</span>
                        {/each}
                      </span>
                    {/if}
                  </span>
                </span>
              </a>
            {/each}
          </div>
        {/if}
      </section>
      {/snippet}

      {#if detail.gitStatus}
        {@const git = detail.gitStatus}
        <section class="page-panel execution-panel wide status-panel-section">
          <div class="panel-heading-row">
            <h2>Status</h2>
            {#if git.hasUpstream !== false && git.behind !== null && git.behind > 0 && onSyncRepo}
              <button
                type="button"
                class="ghost-button"
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
          </div>
          <div class="git-status-bar" aria-label="Git status">
            <div class="git-status-chips">
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
                {/if}
              {/if}
            </div>
          </div>
        </section>
      {/if}

      {#if contextspaceHasContent || contextspaceIssues.length > 0}
        <section class="page-panel execution-panel wide contextspace-panel">
          <div class="panel-heading-row">
            <h2>Contextspace</h2>
            <a
              class="ghost-button"
              href={href(detail.contextspaceHref)}
              data-sveltekit-preload-data="tap"
            >Browse all</a>
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
      {:else if detail.contextspace.length > 0}
        <section class="page-panel execution-panel wide contextspace-panel contextspace-empty-panel">
          <div class="panel-heading-row">
            <h2>Contextspace</h2>
            <a class="ghost-button" href={href(detail.contextspaceHref)} data-sveltekit-preload-data="tap">Set up contextspace →</a>
          </div>
        </section>
      {/if}

      {#if detail.artifacts.length > 0 || artifactIssues.length > 0}
        <section class="page-panel execution-panel wide">
          <h2>Surfaced artifacts</h2>
          {@render degradedIssues(artifactIssues)}
          {#if detail.artifacts.length > 0}
            {@render compactList(detail.artifacts, '')}
          {/if}
        </section>
      {/if}

      {#if (onRetireState && canRetireState(detail)) || (detail.kind === 'worktree' && onRetireWorktree)}
        <section class="danger-zone-panel" aria-label="Danger zone">
          <h2 class="danger-zone-heading">Danger zone</h2>
          <div class="danger-zone-actions">
            {#if onRetireState && canRetireState(detail)}
              <button
                class="ghost-button"
                type="button"
                title="Retire CAR state without deleting git files"
                aria-label={`Clear CAR state for ${detail.title}`}
                onclick={(event) => handleRetireStateClick(event, {
                  kind: detail.kind,
                  id: detail.id,
                  label: shortDetailTitle,
                  hasCarState: detail.hasCarState,
                  unboundManagedThreadCount: detail.unboundManagedThreadCount
                })}
              >
                Clear state
              </button>
            {/if}
            {#if detail.kind === 'worktree' && onRetireWorktree}
              <button
                class="ghost-button danger"
                type="button"
                title="Retire worktree: preserve artifacts, then remove the checkout"
                aria-label={`Retire worktree ${detail.title}`}
                onclick={(event) => handleRetireClick(event, {
                  id: detail.id,
                  label: shortDetailTitle,
                  chatBound: detail.chatBound,
                  cleanupBlockedByChatBinding: detail.cleanupBlockedByChatBinding
                })}
              >
                {@render trashIcon()}
                <span>Retire worktree</span>
              </button>
            {/if}
          </div>
        </section>
      {/if}
    </div>

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
