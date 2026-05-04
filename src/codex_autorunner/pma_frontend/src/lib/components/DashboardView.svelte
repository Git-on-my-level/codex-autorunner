<script lang="ts">
  import type { DashboardAttentionRow, DashboardViewModel } from '$lib/viewModels/dashboard';
  import { dashboardRowMeta } from '$lib/viewModels/dashboard';
  import { statusLabel } from '$lib/viewModels/pmaChat';

  let {
    state,
    dashboard = null,
    errorMessage = null
  }: {
    state: 'loading' | 'error' | 'ready';
    dashboard?: DashboardViewModel | null;
    errorMessage?: string | null;
  } = $props();
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
        <a class={`metric-card ${metric.tone}`} href={metric.href}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
        </a>
      {/each}
    </div>

    {#if !dashboard.hasAnyData}
      <div class="page-panel">
        <h2>No active CAR work</h2>
        <p>Add a repo, create tickets, or start a PMA chat to populate this operational summary.</p>
        <div class="dashboard-actions">
          <a href="/pma">Open PMA</a>
          <a href="/repos">View repos</a>
          <a href="/tickets">View tickets</a>
        </div>
      </div>
    {/if}

    <div class="dashboard-grid">
      <section id="active-runs" class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <h2>Active runs</h2>
          <a href="/pma">PMA chats</a>
        </div>
        {#if dashboard.activeRuns.length === 0}
          <p>No runs are currently active.</p>
        {:else}
          <div class="dashboard-list">
            {#each dashboard.activeRuns as run}
              <article class="dashboard-row run-row">
                <a class="row-main" href={run.primaryHref}>
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
                  {#if run.repoHref}<a href={run.repoHref}>Repo</a>{/if}
                  {#if run.worktreeHref}<a href={run.worktreeHref}>Worktree</a>{/if}
                  {#if run.ticketHref}<a href={run.ticketHref}>Ticket</a>{/if}
                  {#if run.chatHref}<a href={run.chatHref}>Chat</a>{/if}
                </div>
              </article>
            {/each}
          </div>
        {/if}
      </section>

      <section id="waiting-for-me" class="page-panel dashboard-panel">
        <div class="panel-heading-row">
          <h2>Waiting for me</h2>
          <a href="/settings">Approvals</a>
        </div>
        {@render attentionList(dashboard.waitingForMe, 'No approvals, blockers, or unclear requirements are waiting.')}
      </section>

      <section id="failed-blocked" class="page-panel dashboard-panel">
        <div class="panel-heading-row">
          <h2>Failed/blocked</h2>
          <a href="/tickets">Tickets</a>
        </div>
        {@render attentionList(dashboard.failedOrBlocked, 'No failed or blocked work is visible.')}
      </section>

      <section class="page-panel dashboard-panel">
        <div class="panel-heading-row">
          <h2>Repos and worktrees</h2>
          <a href="/repos">All repos</a>
        </div>
        {#if dashboard.repoWorktrees.length === 0}
          <p>No repos or worktrees are registered yet.</p>
        {:else}
          <div class="dashboard-list compact">
            {#each dashboard.repoWorktrees as item}
              <a class="dashboard-row compact-row" href={item.href}>
                <span>
                  <span class="row-title">{item.label}</span>
                  <span class="row-meta">{item.detail} · {statusLabel(item.status)} · {dashboardRowMeta(item)}</span>
                </span>
                <span class="row-counts">{item.activeRuns} runs · {item.openTickets} tickets</span>
              </a>
            {/each}
          </div>
        {/if}
      </section>

      <section class="page-panel dashboard-panel wide">
        <div class="panel-heading-row">
          <h2>Recent activity</h2>
          <a href="/pma">Open PMA</a>
        </div>
        {#if dashboard.recentActivity.length === 0}
          <p>No recent changes or surfaced artifacts are available.</p>
        {:else}
          <div class="dashboard-list activity-list">
            {#each dashboard.recentActivity as activity}
              <a class="dashboard-row activity-row" href={activity.href}>
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
    <p>{emptyText}</p>
  {:else}
    <div class="dashboard-list">
      {#each rows as row}
        <a class={`dashboard-row attention-row ${row.status}`} href={row.primaryHref}>
          <span class="row-title">{row.title}</span>
          <span class="row-meta">{row.kind} · {row.description} · {dashboardRowMeta(row)}</span>
        </a>
      {/each}
    </div>
  {/if}
{/snippet}
