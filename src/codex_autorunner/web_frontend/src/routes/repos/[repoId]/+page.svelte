<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';
  import type { SurfaceArtifact } from '$lib/viewModels/domain';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    void loadRepoDetail();
    refreshTimer = setInterval(() => void loadRepoDetail(false), 10000);
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
  });

  async function loadRepoDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const [repos, worktrees, runs, chats, tickets, contextspace] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listRuns({ repo: repoId }),
      pmaApi.pma.listChats(),
      pmaApi.ticketFlow.listTickets({ repo: repoId }),
      pmaApi.contextspace.listDocuments(repoId)
    ]);
    const primaryError = !repos.ok ? repos.error : !worktrees.ok ? worktrees.error : null;
    if (primaryError) {
      error = primaryError;
      loading = false;
      return;
    }
    const baseIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'Chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null,
      !contextspace.ok ? partialPageIssue('contextspace', 'Contextspace unavailable', contextspace.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    const baseSource = {
      repos: dataOr(repos, []),
      worktrees: dataOr(worktrees, []),
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      tickets: dataOr(tickets, []),
      contextspaceDocs: dataOr(contextspace, []),
      artifacts: [] as SurfaceArtifact[]
    };
    const baseDetail = buildRepoWorktreeDetailViewModel(baseSource, 'repo', repoId);
    if (baseDetail.isMissing) {
      detail = baseDetail;
      sectionIssues = baseIssues;
      loading = false;
      return;
    }
    const artifactResults = await Promise.all(
      baseDetail.currentRuns.map((run) => pmaApi.ticketFlow.listArtifacts(run.id, { repo: repoId }))
    );
    const artifactIssues = artifactResults
      .filter((result): result is { ok: false; error: ApiError } => !result.ok)
      .filter((result) => result.error.status !== 404)
      .map((result) => partialPageIssue('artifacts', 'Surfaced artifacts unavailable', result.error));
    sectionIssues = [...baseIssues, ...artifactIssues];
    const artifacts = artifactResults.flatMap((result) => (result.ok ? result.data : []));
    detail = buildRepoWorktreeDetailViewModel({ ...baseSource, artifacts }, 'repo', repoId);
    loading = false;
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
