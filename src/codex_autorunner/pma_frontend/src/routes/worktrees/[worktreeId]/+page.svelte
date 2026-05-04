<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';
  import type { SurfaceArtifact } from '$lib/viewModels/domain';

  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    void loadWorktreeDetail();
    refreshTimer = setInterval(() => void loadWorktreeDetail(false), 10000);
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
  });

  async function loadWorktreeDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
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
    const baseSource = {
      repos: repos.ok ? repos.data : [],
      worktrees: worktrees.ok ? worktrees.data : [],
      runs: runs.ok ? runs.data : [],
      chats: chats.ok ? chats.data : [],
      tickets: tickets.ok ? tickets.data : [],
      artifacts: [] as SurfaceArtifact[]
    };
    const baseDetail = buildRepoWorktreeDetailViewModel(baseSource, 'worktree', worktreeId);
    const artifactResults = await Promise.all(
      baseDetail.currentRuns.filter((run) => run.logsHref).map((run) => pmaApi.ticketFlow.listArtifacts(run.id))
    );
    const artifacts = artifactResults.flatMap((result) => (result.ok ? result.data : []));
    detail = buildRepoWorktreeDetailViewModel({ ...baseSource, artifacts }, 'worktree', worktreeId);
    loading = false;
  }
</script>

<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  errorMessage={error?.message ?? null}
/>
