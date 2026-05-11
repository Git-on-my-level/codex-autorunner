<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import NewRepoDialog from '$lib/components/NewRepoDialog.svelte';
  import NewWorktreeDialog from '$lib/components/NewWorktreeDialog.svelte';
  import RepoSettingsDialog, { type RepoSettingsTarget } from '$lib/components/RepoSettingsDialog.svelte';
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
    buildRepoWorktreeIndexViewModel({
      repos: selectRepoSummaries(readModelState),
      worktrees: selectWorktreeSummaries(readModelState),
      runs: [],
      chats: [],
      tickets: [],
      artifacts: [],
      ticketsListLoaded: false
    })
  );
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);

  let newRepoOpen = $state(false);
  let newWorktreeOpen = $state(false);
  let newWorktreeTarget = $state<{ id: string; label: string } | null>(null);
  let repoSettingsOpen = $state(false);
  let repoSettingsTarget = $state<RepoSettingsTarget | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadRepos();
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadRepos(): Promise<void> {
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
    if (result.tone === 'success') await loadRepos();
  }

  async function handleArchiveState(target: Parameters<typeof confirmAndArchiveState>[0]): Promise<void> {
    const result = await confirmAndArchiveState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepos();
  }

  function openNewRepo(): void {
    newRepoOpen = true;
  }

  function openNewWorktree(target: { id: string; label: string }): void {
    newWorktreeTarget = target;
    newWorktreeOpen = true;
  }

  function openRepoSettings(target: RepoSettingsTarget): void {
    repoSettingsTarget = target;
    repoSettingsOpen = true;
  }

  async function handleDialogResult(result: ActionNotice): Promise<void> {
    notice = result;
    if (result.tone === 'success') await loadRepos();
  }
</script>

<AutoDismissNotice message={notice?.message ?? null} tone={notice?.tone ?? 'neutral'} />
<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="index"
  {index}
  {sectionIssues}
  onRetry={loadRepos}
  onCleanupWorktree={handleCleanupWorktree}
  onArchiveState={handleArchiveState}
  onCreateRepo={openNewRepo}
  onCreateWorktree={openNewWorktree}
  onOpenRepoSettings={openRepoSettings}
  errorMessage={error?.message ?? null}
/>

<NewRepoDialog bind:open={newRepoOpen} onResult={handleDialogResult} />
<NewWorktreeDialog
  bind:open={newWorktreeOpen}
  target={newWorktreeTarget}
  onResult={handleDialogResult}
/>
<RepoSettingsDialog
  bind:open={repoSettingsOpen}
  target={repoSettingsTarget}
  onResult={handleDialogResult}
/>
