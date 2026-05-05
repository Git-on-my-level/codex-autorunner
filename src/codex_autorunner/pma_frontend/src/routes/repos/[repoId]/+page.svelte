<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
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
    const [repos, worktrees, runs, chats, tickets] = await Promise.all([
      pmaApi.hub.listRepos(),
      pmaApi.hub.listWorktrees(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats(),
      pmaApi.ticketFlow.listTickets()
    ]);
    const primaryError = !repos.ok ? repos.error : !worktrees.ok ? worktrees.error : null;
    if (primaryError) {
      error = primaryError;
      loading = false;
      return;
    }
    const baseIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'PMA chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    const baseSource = {
      repos: dataOr(repos, []),
      worktrees: dataOr(worktrees, []),
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      tickets: dataOr(tickets, []),
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
      baseDetail.currentRuns.filter((run) => run.logsHref).map((run) => pmaApi.ticketFlow.listArtifacts(run.id))
    );
    const artifactIssues = artifactResults
      .filter((result): result is { ok: false; error: ApiError } => !result.ok)
      .map((result) => partialPageIssue('artifacts', 'Surfaced artifacts unavailable', result.error));
    sectionIssues = [...baseIssues, ...artifactIssues];
    const artifacts = artifactResults.flatMap((result) => (result.ok ? result.data : []));
    detail = buildRepoWorktreeDetailViewModel({ ...baseSource, artifacts }, 'repo', repoId);
    loading = false;
  }
</script>

<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {sectionIssues}
  onRetry={() => loadRepoDetail()}
  errorMessage={error?.message ?? null}
/>
