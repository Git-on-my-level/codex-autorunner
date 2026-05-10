<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    buildRepoWorktreeDetailViewModel,
    type RepoWorktreeDetailViewModel
  } from '$lib/viewModels/repoWorktree';
  import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';
  import type { SurfaceArtifact } from '$lib/viewModels/domain';

  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  let detail = $state<RepoWorktreeDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);
  let syncRepoBusy = $state(false);
  let backingRepoId = $state<string | null>(null);
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
    sectionIssues = [];
    backingRepoId = null;
    const [repos, worktrees, runs, chats, tickets] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listRuns({ worktree: worktreeId }),
      pmaApi.pma.listChats(),
      pmaApi.ticketFlow.listTickets({ worktree: worktreeId })
    ]);
    const primaryError = !repos.ok ? repos.error : !worktrees.ok ? worktrees.error : null;
    if (primaryError) {
      error = primaryError;
      loading = false;
      return;
    }
    const worktreeList = dataOr(worktrees, []);
    const matchedWorktree = worktreeList.find((worktree) => worktree.id === worktreeId);
    const redirectTo = legacyWorktreeRedirectPath(stripRuntimeBasePath(page.url.pathname), worktreeId, matchedWorktree?.repoId ?? null);
    if (redirectTo) {
      await goto(href(redirectTo), { replaceState: true });
      return;
    }
    backingRepoId = matchedWorktree?.repoId ?? null;
    const contextspace = await pmaApi.contextspace.listDocuments(worktreeId);
    const baseIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'Chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null,
      !contextspace.ok ? partialPageIssue('contextspace', 'Contextspace unavailable', contextspace.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    const baseSource = {
      repos: dataOr(repos, []),
      worktrees: worktreeList,
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      tickets: dataOr(tickets, []),
      contextspaceDocs: dataOr(contextspace, []),
      artifacts: [] as SurfaceArtifact[]
    };
    const baseDetail = buildRepoWorktreeDetailViewModel(baseSource, 'worktree', worktreeId);
    if (baseDetail.isMissing) {
      detail = baseDetail;
      sectionIssues = baseIssues;
      loading = false;
      return;
    }
    const artifactResults = await Promise.all(
      baseDetail.currentRuns.map((run) => pmaApi.ticketFlow.listArtifacts(run.id, { worktree: worktreeId }))
    );
    const artifactIssues = artifactResults
      .filter((result): result is { ok: false; error: ApiError } => !result.ok)
      .filter((result) => result.error.status !== 404)
      .map((result) => partialPageIssue('artifacts', 'Surfaced artifacts unavailable', result.error));
    sectionIssues = [...baseIssues, ...artifactIssues];
    const artifacts = artifactResults.flatMap((result) => (result.ok ? result.data : []));
    detail = buildRepoWorktreeDetailViewModel({ ...baseSource, artifacts }, 'worktree', worktreeId);
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
