<script lang="ts">
  import { onMount } from 'svelte';
  import DashboardView from '$lib/components/DashboardView.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildDashboardViewModel,
    type DashboardViewModel
  } from '$lib/viewModels/dashboard';

  let dashboard = $state<DashboardViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadDashboard();
  });

  async function loadDashboard(): Promise<void> {
    loading = true;
    error = null;
    const [summary, runs, chats, approvals, repos, worktrees, tickets] = await Promise.all([
      pmaApi.hub.getDashboard(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats(),
      pmaApi.settings.listApprovals(),
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listTickets()
    ]);

    const firstError = [summary, runs, chats, approvals, repos, worktrees, tickets].find((result) => !result.ok);
    if (firstError && !firstError.ok) {
      error = firstError.error;
      loading = false;
      return;
    }

    dashboard = buildDashboardViewModel({
      summary: summary.ok ? summary.data : null,
      runs: runs.ok ? runs.data : [],
      chats: chats.ok ? chats.data : [],
      approvals: approvals.ok ? approvals.data : [],
      repos: repos.ok ? repos.data : [],
      worktrees: worktrees.ok ? worktrees.data : [],
      tickets: tickets.ok ? tickets.data : []
    });
    loading = false;
  }
</script>

<DashboardView
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  {dashboard}
  errorMessage={error?.message ?? null}
/>
