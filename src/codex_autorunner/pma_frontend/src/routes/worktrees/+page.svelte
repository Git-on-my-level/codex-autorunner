<script lang="ts">
  import { onMount } from 'svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let index = $state<RepoWorktreeIndexViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);

  onMount(() => {
    void loadWorktrees();
  });

  async function loadWorktrees(): Promise<void> {
    loading = true;
    error = null;
    sectionIssues = [];
    const [repos, worktrees, runs, chats, tickets] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats(),
      pmaApi.ticketFlow.listTickets()
    ]);
    const primaryError = !repos.ok ? repos.error : !worktrees.ok ? worktrees.error : null;
    if (primaryError) {
      error = primaryError;
      loading = false;
      return;
    }
    sectionIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'PMA chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    index = buildRepoWorktreeIndexViewModel(
      {
        repos: dataOr(repos, []),
        worktrees: dataOr(worktrees, []),
        runs: dataOr(runs, []),
        chats: dataOr(chats, []),
        tickets: dataOr(tickets, []),
        artifacts: []
      },
      'worktree'
    );
    loading = false;
  }
</script>

<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="index"
  {index}
  {sectionIssues}
  onRetry={loadWorktrees}
  errorMessage={error?.message ?? null}
/>
