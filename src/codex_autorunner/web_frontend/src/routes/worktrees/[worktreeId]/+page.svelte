<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import { mapContextspaceDocument, mapPmaChatSummary, mapPmaRunProgress, mapSurfaceArtifact, mapTicketSummary, mapWorktreeSummary } from '$lib/viewModels/domain';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';
  import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';

  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);
  let backingRepoId = $state<string | null>(null);

  onMount(() => {
    void loadWorktreeDetail();
  });

  async function loadWorktreeDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    backingRepoId = null;
    const snapshot = await pmaApi.readModels.worktreeDetail(worktreeId);
    if (!snapshot.ok) {
      error = snapshot.error;
      loading = false;
      return;
    }
    const payload = snapshot.data;
    const worktreeList = [mapWorktreeSummary(payload.identity)];
    const matchedWorktree = worktreeList.find((worktree) => worktree.id === worktreeId);
    const redirectTo = legacyWorktreeRedirectPath(stripRuntimeBasePath(page.url.pathname), worktreeId, matchedWorktree?.repoId ?? null);
    if (redirectTo) {
      await goto(href(redirectTo), { replaceState: true });
      return;
    }
    backingRepoId = matchedWorktree?.repoId ?? null;
    const baseIssues: PartialPageIssue[] = [];
    const baseSource = {
      repos: [],
      worktrees: worktreeList,
      runs: payload.scopedRuns.map(mapPmaRunProgress),
      chats: payload.scopedChats.map(mapPmaChatSummary),
      tickets: payload.scopedTickets.map(mapTicketSummary),
      contextspaceDocs: payload.contextspaceSummary.map(mapContextspaceDocument),
      artifacts: payload.currentArtifacts.map(mapSurfaceArtifact)
    };
    const baseDetail = buildRepoWorktreeDetailViewModel(baseSource, 'worktree', worktreeId);
    if (baseDetail.isMissing) {
      detail = baseDetail;
      sectionIssues = baseIssues;
      loading = false;
      return;
    }
    sectionIssues = baseIssues;
    detail = baseDetail;
    loading = false;
  }

  async function handleCleanupWorktree(target: Parameters<typeof confirmAndCleanupWorktree>[0]): Promise<void> {
    const result = await confirmAndCleanupWorktree(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadWorktreeDetail();
  }

  async function handleArchiveState(target: Parameters<typeof confirmAndArchiveState>[0]): Promise<void> {
    const result = await confirmAndArchiveState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadWorktreeDetail();
  }

  async function handleSyncRepo(): Promise<void> {
    if (syncRepoBusy) return;
    const repoId = backingRepoId;
    if (!repoId) {
      notice = { tone: 'danger', message: 'Could not resolve parent repo for sync.' };
      return;
    }
    syncRepoBusy = true;
    try {
      const result = await pmaApi.hub.syncRepoMain(repoId);
      if (!result.ok) {
        notice = { tone: 'danger', message: result.error.message };
        return;
      }
      notice = { tone: 'success', message: 'Synced default branch with origin.' };
      await loadWorktreeDetail(false);
    } finally {
      syncRepoBusy = false;
    }
  }
</script>

<AutoDismissNotice message={notice?.message ?? null} tone={notice?.tone ?? 'neutral'} />
<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {sectionIssues}
  onRetry={() => loadWorktreeDetail()}
  onCleanupWorktree={handleCleanupWorktree}
  onArchiveState={handleArchiveState}
  onSyncRepo={handleSyncRepo}
  syncRepoBusy={syncRepoBusy}
  errorMessage={error?.message ?? null}
/>
