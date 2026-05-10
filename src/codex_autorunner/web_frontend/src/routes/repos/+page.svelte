<script lang="ts">
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import NewRepoDialog from '$lib/components/NewRepoDialog.svelte';
  import NewWorktreeDialog from '$lib/components/NewWorktreeDialog.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let index = $state<RepoWorktreeIndexViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);

  let newRepoOpen = $state(false);
  let newWorktreeOpen = $state(false);
  let newWorktreeTarget = $state<{ id: string; label: string } | null>(null);

  onMount(() => {
    void loadRepos();
  });

  async function loadRepos(): Promise<void> {
    loading = true;
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
    sectionIssues = [
      !runs.ok ? partialPageIssue('current_run', 'Active runs unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('current_run', 'PMA chats unavailable', chats.error) : null,
      !tickets.ok ? partialPageIssue('tickets', 'Ticket queue unavailable', tickets.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    index = buildRepoWorktreeIndexViewModel({
      repos: dataOr(repos, []),
      worktrees: dataOr(worktrees, []),
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      tickets: dataOr(tickets, []),
      artifacts: [],
      ticketsListLoaded: tickets.ok
    });
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
  errorMessage={error?.message ?? null}
/>

<NewRepoDialog bind:open={newRepoOpen} onResult={handleDialogResult} />
<NewWorktreeDialog
  bind:open={newWorktreeOpen}
  target={newWorktreeTarget}
  onResult={handleDialogResult}
/>
