<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import { ensureRepoDetailLoaded, readModelEntityStore } from '$lib/data';
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

  let { data = { repoId: '', result: { status: 'cold' as const, tags: [] } } } = $props();
  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state<boolean>(data.result.status === 'cold' && !readModelEntityStore.snapshot().repoDetails[repoId]);
  let error = $state<ApiError | null>(data.result.status === 'error' ? data.result.error : null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);

  function buildDetailFromSnapshot(snapshot: Record<string, unknown>): RepoWorktreeDetailViewModel {
    const payload = snapshot as {
      identity: Record<string, unknown>;
      topology: Record<string, unknown>;
      scopedRuns: Record<string, unknown>[];
      scopedChats: Record<string, unknown>[];
      scopedTickets: Record<string, unknown>[];
      contextspaceSummary: Record<string, unknown>[];
      currentArtifacts: Record<string, unknown>[];
    };
    const baseSource = {
      repos: [mapRepoSummary(payload.identity)],
      worktrees: childrenFromTopology(payload.topology).map(mapWorktreeSummary),
      runs: payload.scopedRuns.map(mapPmaRunProgress),
      chats: payload.scopedChats.map(mapPmaChatSummary),
      tickets: payload.scopedTickets.map(mapTicketSummary),
      contextspaceDocs: payload.contextspaceSummary.map(mapContextspaceDocument),
      artifacts: payload.currentArtifacts.map(mapSurfaceArtifact)
    };
    return buildRepoWorktreeDetailViewModel(baseSource, 'repo', repoId);
  }

  onMount(() => {
    const snapshot = readModelEntityStore.snapshot().repoDetails[repoId];
    if (snapshot) {
      detail = buildDetailFromSnapshot(snapshot as unknown as Record<string, unknown>);
    }
    loading = false;
  });

  async function loadRepoDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const result = await ensureRepoDetailLoaded(repoId, { refresh: true });
    if (result.status === 'error') {
      error = result.error;
      loading = false;
      return;
    }
    const snapshot = readModelEntityStore.snapshot().repoDetails[repoId];
    if (snapshot) {
      detail = buildDetailFromSnapshot(snapshot as unknown as Record<string, unknown>);
    }
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
