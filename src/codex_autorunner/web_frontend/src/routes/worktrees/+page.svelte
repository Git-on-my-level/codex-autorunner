<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { type ApiError } from '$lib/api/client';
  import { ensureRepoWorktreeIndexLoaded, readModelEntityStore, selectRepoSummaries, selectWorktreeSummaries } from '$lib/data';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let { data = { status: 'cold' as const, tags: [] } } = $props();
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
  let refreshing = $state<boolean>(false);
  let refreshError = $state<ApiError | null>(null);
  const loading = $derived(data.status === 'cold' || refreshing);
  const error = $derived(refreshError ?? (data.status === 'error' ? data.error : null));
  let notice = $state<ActionNotice | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadWorktrees(): Promise<void> {
    refreshing = true;
    refreshError = null;
    const result = await ensureRepoWorktreeIndexLoaded({ refresh: true });
    if (result.status === 'error') {
      refreshError = result.error;
    }
    refreshing = false;
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
  onRetry={loadWorktrees}
  onCleanupWorktree={handleCleanupWorktree}
  onArchiveState={handleArchiveState}
  errorMessage={error?.message ?? null}
/>
