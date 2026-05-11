<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import {
    mapContextspaceDocument,
    mapPmaChatSummary,
    mapPmaRunProgress,
    mapRepoSummary,
    mapSurfaceArtifact,
    mapTicketSummary,
    mapWorktreeSummary
  } from '$lib/viewModels/domain';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);

  onMount(() => {
    void loadRepoDetail();
  });

  async function loadRepoDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const snapshot = await pmaApi.readModels.repoDetail(repoId);
    if (!snapshot.ok) {
      error = snapshot.error;
      loading = false;
      return;
    }
    const payload = snapshot.data;
    const baseIssues: PartialPageIssue[] = [];
    const baseSource = {
      repos: [mapRepoSummary(payload.identity)],
      worktrees: childrenFromTopology(payload.topology).map(mapWorktreeSummary),
      runs: payload.scopedRuns.map(mapPmaRunProgress),
      chats: payload.scopedChats.map(mapPmaChatSummary),
      tickets: payload.scopedTickets.map(mapTicketSummary),
      contextspaceDocs: payload.contextspaceSummary.map(mapContextspaceDocument),
      artifacts: payload.currentArtifacts.map(mapSurfaceArtifact)
    };
    const baseDetail = buildRepoWorktreeDetailViewModel(baseSource, 'repo', repoId);
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

  function childrenFromTopology(topology: Record<string, unknown>): JsonRecord[] {
    return Array.isArray(topology.children) ? (topology.children as JsonRecord[]) : [];
  }

  async function handleCleanupWorktree(target: Parameters<typeof confirmAndCleanupWorktree>[0]): Promise<void> {
    const result = await confirmAndCleanupWorktree(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepoDetail();
  }

  async function handleArchiveState(target: Parameters<typeof confirmAndArchiveState>[0]): Promise<void> {
    const result = await confirmAndArchiveState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepoDetail();
  }

  async function handleSyncRepo(): Promise<void> {
    if (syncRepoBusy) return;
    syncRepoBusy = true;
    try {
      const result = await pmaApi.hub.syncRepoMain(repoId);
      if (!result.ok) {
        notice = { tone: 'danger', message: result.error.message };
        return;
      }
      notice = { tone: 'success', message: 'Synced default branch with origin.' };
      await loadRepoDetail(false);
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
  onRetry={() => loadRepoDetail()}
  onCleanupWorktree={handleCleanupWorktree}
  onArchiveState={handleArchiveState}
  onSyncRepo={handleSyncRepo}
  syncRepoBusy={syncRepoBusy}
  errorMessage={error?.message ?? null}
/>
