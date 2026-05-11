<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import { readModelEntityStore, selectRepoSummaries, selectWorktreeSummaries } from '$lib/data';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  const index = $derived<RepoWorktreeIndexViewModel | null>(
    buildRepoWorktreeIndexViewModel(
      {
        repos: selectRepoSummaries(readModelState),
        worktrees: selectWorktreeSummaries(readModelState),
        runs: [],
        chats: [],
        tickets: [],
        artifacts: [],
        ticketsListLoaded: false
      },
      'worktree'
    )
  );
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadWorktrees();
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadWorktrees(): Promise<void> {
    loading = true;
    error = null;
    sectionIssues = [];
    const [topology, runtime] = await Promise.all([
      pmaApi.readModels.repoWorktreeTopology('all', 200),
      pmaApi.readModels.repoWorktreeRuntime('all', 200)
    ]);
    if (!topology.ok) {
      error = topology.error;
      loading = false;
      return;
    }
    if (!runtime.ok) {
      error = runtime.error;
      loading = false;
      return;
    }
    readModelEntityStore.applyRepoWorktreeTopologySnapshot(topology.data);
    readModelEntityStore.applyRepoWorktreeRuntimeSnapshot(runtime.data);
    sectionIssues = [];
    loading = false;
  }

  async function handleCleanupWorktree(target: Parameters<typeof confirmAndCleanupWorktree>[0]): Promise<void> {
    const result = await confirmAndCleanupWorktree(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadWorktrees();
  }

  async function handleArchiveState(target: Parameters<typeof confirmAndArchiveState>[0]): Promise<void> {
    const result = await confirmAndArchiveState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadWorktrees();
  }
</script>

<AutoDismissNotice message={notice?.message ?? null} tone={notice?.tone ?? 'neutral'} />
<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="index"
  {index}
  {sectionIssues}
  onRetry={loadWorktrees}
  onCleanupWorktree={handleCleanupWorktree}
  onArchiveState={handleArchiveState}
  errorMessage={error?.message ?? null}
/>
