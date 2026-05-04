<script lang="ts">
  import { onMount } from 'svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let index = $state<RepoWorktreeIndexViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadRepos();
  });

  async function loadRepos(): Promise<void> {
    loading = true;
    error = null;
    const [repos, worktrees, runs, chats, tickets] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats(),
      pmaApi.ticketFlow.listTickets()
    ]);
    const firstError = [repos, worktrees, runs, chats, tickets].find((result) => !result.ok);
    if (firstError && !firstError.ok) {
      error = firstError.error;
      loading = false;
      return;
    }
    index = buildRepoWorktreeIndexViewModel({
      repos: repos.ok ? repos.data : [],
      worktrees: worktrees.ok ? worktrees.data : [],
      runs: runs.ok ? runs.data : [],
      chats: chats.ok ? chats.data : [],
      tickets: tickets.ok ? tickets.data : [],
      artifacts: []
    });
    loading = false;
  }
</script>

<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="index"
  {index}
  errorMessage={error?.message ?? null}
/>
