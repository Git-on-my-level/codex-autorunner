<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
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
    const baseIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'PMA chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    const baseSource = {
      repos: dataOr(repos, []),
      worktrees: worktreeList,
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      tickets: dataOr(tickets, []),
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
      .map((result) => partialPageIssue('artifacts', 'Surfaced artifacts unavailable', result.error));
    sectionIssues = [...baseIssues, ...artifactIssues];
    const artifacts = artifactResults.flatMap((result) => (result.ok ? result.data : []));
    detail = buildRepoWorktreeDetailViewModel({ ...baseSource, artifacts }, 'worktree', worktreeId);
    loading = false;
  }
</script>

<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {sectionIssues}
  onRetry={() => loadWorktreeDetail()}
  errorMessage={error?.message ?? null}
/>
