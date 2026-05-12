<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import { mapContextspaceDocument, mapPmaChatSummary, mapPmaRunProgress, mapSurfaceArtifact, mapTicketSummary, mapWorktreeSummary } from '$lib/viewModels/domain';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';
  import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';
  import { ensureWorktreeDetailLoaded, invalidateReadModelTags, readModelEntityStore, readModelEntityTags } from '$lib/data';

  let { data = { worktreeId: '', result: { status: 'cold' as const, tags: [] } } } = $props();
  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state<boolean>(true);
  let error = $state<ApiError | null>(null);

  $effect(() => {
    const r = data.result;
    const id = worktreeId;
    loading = r.status === 'cold' && !readModelEntityStore.snapshot().worktreeDetails[id];
    error = r.status === 'error' ? r.error : null;
  });
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);
  let backingRepoId = $state<string | null>(null);

  function buildDetailFromSnapshot(snapshot: Record<string, unknown>): RepoWorktreeDetailViewModel {
    const payload = snapshot as {
      identity: Record<string, unknown>;
      scopedRuns: Record<string, unknown>[];
      scopedChats: Record<string, unknown>[];
      scopedTickets: Record<string, unknown>[];
      contextspaceSummary: Record<string, unknown>[];
      currentArtifacts: Record<string, unknown>[];
    };
    const worktreeList = [mapWorktreeSummary(payload.identity)];
    const matchedWorktree = worktreeList.find((worktree) => worktree.id === worktreeId);
    backingRepoId = matchedWorktree?.repoId ?? null;
    const baseSource = {
      repos: [],
      worktrees: worktreeList,
      runs: payload.scopedRuns.map(mapPmaRunProgress),
      chats: payload.scopedChats.map(mapPmaChatSummary),
      tickets: payload.scopedTickets.map(mapTicketSummary),
      contextspaceDocs: payload.contextspaceSummary.map(mapContextspaceDocument),
      artifacts: payload.currentArtifacts.map(mapSurfaceArtifact)
    };
    return buildRepoWorktreeDetailViewModel(baseSource, 'worktree', worktreeId);
  }

  onMount(() => {
    const snapshot = readModelEntityStore.snapshot().worktreeDetails[worktreeId];
    if (snapshot) {
      const worktreeList = [mapWorktreeSummary(snapshot.identity as Record<string, unknown>)];
      const matchedWorktree = worktreeList.find((worktree) => worktree.id === worktreeId);
      const redirectTo = legacyWorktreeRedirectPath(stripRuntimeBasePath(page.url.pathname), worktreeId, matchedWorktree?.repoId ?? null);
      if (redirectTo) {
        void goto(href(redirectTo), { replaceState: true });
        return;
      }
      detail = buildDetailFromSnapshot(snapshot as unknown as Record<string, unknown>);
    }
    loading = false;
  });

  async function loadWorktreeDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    backingRepoId = null;
    const result = await ensureWorktreeDetailLoaded(worktreeId, { refresh: true });
    if (result.status === 'error') {
      error = result.error;
      loading = false;
      return;
    }
    const snapshot = readModelEntityStore.snapshot().worktreeDetails[worktreeId];
    if (snapshot) {
      const worktreeList = [mapWorktreeSummary(snapshot.identity as Record<string, unknown>)];
      const matchedWorktree = worktreeList.find((worktree) => worktree.id === worktreeId);
      const redirectTo = legacyWorktreeRedirectPath(stripRuntimeBasePath(page.url.pathname), worktreeId, matchedWorktree?.repoId ?? null);
      if (redirectTo) {
        await goto(href(redirectTo), { replaceState: true });
        return;
      }
      detail = buildDetailFromSnapshot(snapshot as unknown as Record<string, unknown>);
    }
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
      await invalidateReadModelTags([
        readModelEntityTags.repoWorktreeIndex,
        readModelEntityTags.repo(repoId),
        readModelEntityTags.worktree(worktreeId)
      ]);
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
