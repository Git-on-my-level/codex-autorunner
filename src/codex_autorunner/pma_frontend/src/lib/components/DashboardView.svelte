<script lang="ts">
  import type { DashboardAttentionRow, DashboardViewModel } from '$lib/viewModels/dashboard';
  import { dashboardRowMeta } from '$lib/viewModels/dashboard';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { PartialPageIssue } from '$lib/api/client';

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
  <div class="section-heading">
    <p class="eyebrow">Overview</p>
    <h1>Dashboard</h1>
  </div>

  {#if state === 'loading'}
    <div class="state-panel dashboard-state">Loading dashboard...</div>
  {:else if state === 'error'}
    <div class="state-panel error dashboard-state">Could not load dashboard. {errorMessage}</div>
  {:else if dashboard}
    <div class="summary-grid">
      {#each dashboard.metrics as metric}
        <a class={`metric-card ${metric.tone}`} href={href(metric.href)}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
        </a>
      {/each}
    </div>

    {#if !dashboard.hasAnyData}
      <div class="state-panel empty-state operational-empty">
        <h2>No active CAR work</h2>
        <p>Start from PMA or open the workspace queues; dashboard signals will fill in as runs, tickets, and artifacts appear.</p>
        <div class="dashboard-actions">
          <a href={href('/tickets')}>View workspace tickets</a>
        </div>
      </div>
    {/if}

    <div class="dashboard-grid">
      <section id="active-runs" class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <h2>Active runs</h2>
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
              <article class="dashboard-row run-row">
                <a class="row-main" href={href(run.primaryHref)}>
                  <span class="row-title">{run.title}</span>
                  <span class="row-meta">
                    {statusLabel(run.status)}
                    {#if run.phase} · {run.phase}{/if}
                    · {dashboardRowMeta(run)}
                  </span>
                  <span class="progress-track" aria-label={`${run.progress} percent complete`}>
                    <span style={`width: ${run.progress}%`}></span>
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
          <a href={href('/tickets')}>Workspace tickets</a>
        </div>
        {@render degradedIssues(failedIssues)}
        {@render attentionList(dashboard.failedOrBlocked, 'No failed or blocked work is visible.')}
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
                <span class={`activity-kind ${activity.artifact?.kind ?? 'progress'}`}>
                  {activity.artifact?.kind ?? 'activity'}
                </span>
                <span>
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
