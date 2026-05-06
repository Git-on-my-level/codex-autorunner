<script lang="ts">
  import { onMount } from 'svelte';
  import DashboardView from '$lib/components/DashboardView.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildDashboardViewModel,
    type DashboardViewModel
  } from '$lib/viewModels/dashboard';

  let dashboard = $state<DashboardViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);

  onMount(() => {
    void loadDashboard();
  });

  async function loadDashboard(): Promise<void> {
    loading = true;
    error = null;
    sectionIssues = [];
    const [summary, runs, chats, approvals, tickets, repos, worktrees] = await Promise.all([
      pmaApi.hub.getDashboard(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats(),
      pmaApi.settings.listApprovals(),
      pmaApi.ticketFlow.listTickets(),
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees()
    ]);

    const results = [summary, runs, chats, approvals, tickets];
    const firstError = results.find((result) => !result.ok);
    if (results.every((result) => !result.ok) && firstError && !firstError.ok) {
      error = firstError.error;
      loading = false;
      return;
    }

    sectionIssues = [
      !summary.ok ? partialPageIssue('recent_activity', 'Dashboard summary unavailable', summary.error) : null,
      !runs.ok ? partialPageIssue('active_runs', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('active_runs', 'PMA chats unavailable', chats.error) : null,
      !approvals.ok ? partialPageIssue('waiting_for_me', 'Approvals unavailable', approvals.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));

    dashboard = buildDashboardViewModel({
      summary: dataOr(summary, null),
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      approvals: dataOr(approvals, []),
      tickets: dataOr(tickets, []),
      repos: dataOr(repos, []),
      worktrees: dataOr(worktrees, [])
    });
    loading = false;
  }
</script>

<DashboardView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {dashboard}
  {sectionIssues}
  onRetry={loadDashboard}
  errorMessage={error?.message ?? null}
/>
