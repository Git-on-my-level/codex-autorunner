<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import NewRepoDialog from '$lib/components/NewRepoDialog.svelte';
  import NewWorktreeDialog from '$lib/components/NewWorktreeDialog.svelte';
  import RepoSettingsDialog, { type RepoSettingsTarget } from '$lib/components/RepoSettingsDialog.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { webApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import { ensureRepoWorktreeIndexLoaded, invalidateReadModelTags, readModelEntityStore, readModelEntityTags, selectRepoSummaries, selectWorktreeSummaries } from '$lib/data';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let { data = { status: 'cold' as const, tags: [] } } = $props();
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
  let refreshing = $state<boolean>(false);
  let refreshError = $state<ApiError | null>(null);
  const loading = $derived(data.status === 'cold' || refreshing);
  const error = $derived(refreshError ?? (data.status === 'error' ? data.error : null));
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
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadRepos(): Promise<void> {
    refreshing = true;
    refreshError = null;
    sectionIssues = [];
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
    if (result.tone === 'success') await loadRepos();
  }

  async function handleArchiveState(target: Parameters<typeof confirmAndArchiveState>[0]): Promise<void> {
    const result = await confirmAndArchiveState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepos();
  }

  async function handleRepoPin(target: { id: string; pinned: boolean }): Promise<void> {
    const result = await webApi.hub.setRepoPinned(target.id, target.pinned);
    if (!result.ok) {
      notice = { tone: 'danger', message: result.error.message };
      return;
    }
    await invalidateReadModelTags([
      readModelEntityTags.repoWorktreeIndex,
      readModelEntityTags.repo(target.id)
    ]);
    await loadRepos();
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
    if (result.tone !== 'success') return;
    await loadRepos();
    if (result.navigateTo) void goto(result.navigateTo);
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
  onRepoPin={handleRepoPin}
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
