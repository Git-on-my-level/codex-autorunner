<script lang="ts">
  import type { DashboardAttentionRow, DashboardViewModel } from '$lib/viewModels/dashboard';
  import { dashboardRowMeta } from '$lib/viewModels/dashboard';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { PartialPageIssue } from '$lib/api/client';
  import PageHero from './PageHero.svelte';

  let {
    state,
    dashboard = null,
    errorMessage = null,
    sectionIssues = [],
    onRetry = undefined
  }: {
    state: 'loading' | 'error' | 'ready';
    dashboard?: DashboardViewModel | null;
    errorMessage?: string | null;
    sectionIssues?: PartialPageIssue[];
    onRetry?: (() => void) | undefined;
  } = $props();

  const activeRunIssues = $derived(sectionIssues.filter((issue) => issue.id === 'active_runs'));
  const waitingIssues = $derived(sectionIssues.filter((issue) => issue.id === 'waiting_for_me'));
  const failedIssues = $derived(sectionIssues.filter((issue) => issue.id === 'tickets'));
  const activityIssues = $derived(sectionIssues.filter((issue) => issue.id === 'recent_activity'));
</script>

<section class="page-stack dashboard-page">
  <PageHero
    title="Dashboard"
    subtitle="Active CAR work, approvals, and recent activity at a glance."
  >
    {#snippet stats()}
      {#if dashboard}
        <dl class="hero-stats" aria-label="Dashboard summary">
          {#each dashboard.metrics as metric}
            <a class={metric.value > 0 ? metric.tone : 'neutral'} href={href(metric.href)}>
              <dd>{metric.value}</dd>
              <dt>{metric.label}</dt>
            </a>
          {/each}
        </dl>
      {/if}
    {/snippet}
  </PageHero>

  {#if state === 'loading'}
    <div class="state-panel dashboard-state">Loading dashboard...</div>
  {:else if state === 'error'}
    <div class="state-panel error dashboard-state">Could not load dashboard. {errorMessage}</div>
  {:else if dashboard}
    {#if !dashboard.hasAnyData}
      <div class="state-panel empty-state operational-empty">
        <h2>No active CAR work</h2>
        <p>Start from PMA or open the workspace queues; dashboard signals will fill in as runs, tickets, and artifacts appear.</p>
      </div>
    {/if}

    <div class="dashboard-grid">
      <section id="waiting-for-me" class="page-panel dashboard-panel">
        <div class="panel-heading-row">
          <h2>Waiting for me</h2>
          <a href={href('/settings')}>Approvals</a>
        </div>
        {@render degradedIssues(waitingIssues)}
        {@render attentionList(dashboard.waitingForMe, 'No approvals or blockers are waiting.')}
      </section>

      <section id="failed-blocked" class="page-panel dashboard-panel">
        <div class="panel-heading-row">
          <h2>Failed/blocked</h2>
          <a href={href('/repos')}>All repos</a>
        </div>
        {@render degradedIssues(failedIssues)}
        {@render attentionList(dashboard.failedOrBlocked, 'No failed or blocked work is visible.')}
      </section>

      <section id="active-runs" class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <div class="panel-heading-stack">
            <h2>Active runs</h2>
            <p class="panel-heading-sub">Live PMA chats and runs. Ticket-flow queues appear below.</p>
          </div>
        </div>
        {@render degradedIssues(activeRunIssues)}
        {#if dashboard.activeRuns.length === 0}
          <div class="state-panel empty-state compact-empty">
            <strong>No active runs</strong>
            <p>Queue a ticket or send PMA a task to start work.</p>
          </div>
        {:else}
          <div class="dashboard-list">
            {#each dashboard.activeRuns as run}
              {@const indeterminate = run.status === 'running' && run.progressPercent === null}
              <article class={`dashboard-row run-row status-${run.status}`}>
                <a class="row-main" href={href(run.primaryHref)}>
                  <span class="row-title">
                    <span class={`status-dot status-${run.status}`} aria-hidden="true"></span>
                    {run.title}
                    <small class="row-id">#{run.id.slice(0, 6)}</small>
                  </span>
                  <span class="row-meta">
                    <span class="kind-chip">{run.kindLabel}</span>
                    {statusLabel(run.status)}
                    {#if run.phase} · {run.phase}{/if}
                    {#if run.ticketId} · <code>{run.ticketId}</code>{/if}
                    {#if run.elapsedLabel} · {run.elapsedLabel}{/if}
                    · {dashboardRowMeta(run)}
                  </span>
                  <span
                    class={`progress-track status-${run.status} ${indeterminate ? 'indeterminate' : ''} ${run.progressPercent === null && !indeterminate ? 'idle' : ''}`}
                    aria-label={run.progressPercent === null ? statusLabel(run.status) : `${run.progressPercent} percent complete`}
                  >
                    {#if run.progressPercent !== null}
                      <span style={`width: ${run.progressPercent}%`}></span>
                    {:else if indeterminate}
                      <span class="indeterminate-bar"></span>
                    {/if}
                  </span>
                </a>
                <div class="row-links">
                  {#if run.ticketHref}<a href={href(run.ticketHref)}>Ticket</a>{/if}
                  {#if run.chatHref}<a href={href(run.chatHref)}>Chat</a>{/if}
                </div>
              </article>
            {/each}
          </div>
        {/if}
      </section>

      <section id="queues" class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <div class="panel-heading-stack">
            <h2>Ticket-flow queues</h2>
            <p class="panel-heading-sub">Per-repo and per-worktree ticket pipelines.</p>
          </div>
          <a href={href('/repos')}>All repos</a>
        </div>
        {#if dashboard.queues.length === 0}
          <div class="state-panel empty-state compact-empty">
            <strong>No queues yet</strong>
            <p>Repo and worktree ticket queues appear here once work is registered.</p>
          </div>
        {:else}
          <div class="dashboard-list queue-list">
            {#each dashboard.queues as queue}
              <a class={`dashboard-row queue-row status-${queue.flowStatus.signal}`} href={href(queue.href)}>
                <span class="row-main">
                  <span class="row-title">
                    <span class={`status-pill ${queue.flowStatus.signal}`}>{queue.flowStatus.statusLabel}</span>
                    <strong>{queue.label}</strong>
                    <small class="row-id">{queue.kind}</small>
                  </span>
                  <span class="row-meta">
                    {queue.doneCount}/{queue.totalCount} done
                    {#if queue.flowStatus.currentTicketLabel && queue.flowStatus.currentTicketLabel !== 'None'}
                      · current: {queue.flowStatus.currentTicketLabel}
                    {/if}
                    {#if queue.flowStatus.reasonLabel && queue.flowStatus.reasonLabel !== 'No reason reported'}
                      · {queue.flowStatus.reasonLabel}
                    {/if}
                  </span>
                </span>
              </a>
            {/each}
          </div>
        {/if}
      </section>

      <section class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <h2>Recent activity</h2>
        </div>
        {@render degradedIssues(activityIssues)}
        {#if dashboard.recentActivity.length === 0}
          <div class="state-panel empty-state compact-empty">
            <strong>No recent activity</strong>
            <p>Completed PMA work, screenshots, previews, and test summaries will appear here.</p>
          </div>
        {:else}
          <div class="dashboard-list activity-list">
            {#each dashboard.recentActivity as activity}
              <a class="dashboard-row activity-row" href={href(activity.href)}>
                {#if activity.artifact?.kind}
                  <span class={`activity-kind ${activity.artifact.kind}`}>
                    {activity.artifact.kind}
                  </span>
                {/if}
                <span class="activity-body">
                  <span class="row-title">{activity.title}</span>
                  <span class="row-meta">{activity.summary} · {dashboardRowMeta({ updatedAt: activity.createdAt })}</span>
                </span>
              </a>
            {/each}
          </div>
        {/if}
      </section>
    </div>
  {/if}
</section>

{#snippet attentionList(rows: DashboardAttentionRow[], emptyText: string)}
  {#if rows.length === 0}
    <div class="state-panel empty-state compact-empty">
      <strong>Clear</strong>
      <p>{emptyText}</p>
    </div>
  {:else}
    <div class="dashboard-list">
      {#each rows as row}
        <a class={`dashboard-row attention-row ${row.status}`} href={href(row.primaryHref)}>
          <span class="row-title">{row.title}</span>
          <span class="row-meta">{row.kind} · {row.description} · {dashboardRowMeta(row)}</span>
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
