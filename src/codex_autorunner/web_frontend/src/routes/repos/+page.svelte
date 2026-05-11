<script lang="ts">
  import { onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import NewRepoDialog from '$lib/components/NewRepoDialog.svelte';
  import NewWorktreeDialog from '$lib/components/NewWorktreeDialog.svelte';
  import RepoSettingsDialog, { type RepoSettingsTarget } from '$lib/components/RepoSettingsDialog.svelte';
  import { confirmAndArchiveState, confirmAndCleanupWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import type { RepoTopology, RuntimeProjection, WorktreeTopology } from '$lib/api/readModelContracts';
  import { mapRepoSummary, mapWorktreeSummary } from '$lib/viewModels/domain';
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
  let repoSettingsOpen = $state(false);
  let repoSettingsTarget = $state<RepoSettingsTarget | null>(null);

  onMount(() => {
    void loadRepos();
  });

  async function loadRepos(): Promise<void> {
    loading = true;
    error = null;
    sectionIssues = [];
    const [topology, runtime] = await Promise.all([
      pmaApi.readModels.repoWorktreeTopology('all', 200),
      pmaApi.readModels.repoWorktreeRuntime('all', 200)
    ]);
    if (!topology.ok) {
      error = topology.error;
      loading = false;
      return;
    }
    if (!runtime.ok) {
      error = runtime.error;
      loading = false;
      return;
    }
    sectionIssues = [];
    const runtimeById = new Map<string, RuntimeProjection>(runtime.data.runtime.map((row: RuntimeProjection) => [row.entityId, row]));
    const repos = topology.data.repos.map((repo: RepoTopology) =>
      mapRepoSummary({
        id: repo.repoId,
        name: repo.label,
        path: repo.path,
        kind: 'base',
        worktree_count: repo.childWorktreeIds.length,
        ...runtimeRaw(runtimeById.get(repo.repoId))
      })
    );
    const worktrees = topology.data.worktrees.map((worktree: WorktreeTopology) =>
      mapWorktreeSummary({
        id: worktree.worktreeId,
        name: worktree.label,
        path: worktree.path,
        kind: 'worktree',
        worktree_of: worktree.repoId,
        branch: worktree.branch,
        ...runtimeRaw(runtimeById.get(worktree.worktreeId))
      })
    );
    index = buildRepoWorktreeIndexViewModel({
      repos,
      worktrees,
      runs: [],
      chats: [],
      tickets: [],
      artifacts: [],
      ticketsListLoaded: false
    });
    loading = false;
  }

  function runtimeRaw(row: RuntimeProjection | undefined): JsonRecord {
    return row
      ? {
          active_runs: row.activeRunId ? 1 : 0,
          open_tickets: row.waitingTicketCount + row.runningTicketCount,
          ticket_flow_display: {
            status: row.activeRunStatus,
            is_active: Boolean(row.activeRunId),
            total_count: row.waitingTicketCount + row.runningTicketCount,
            done_count: 0,
            run_id: row.activeRunId
          },
          git_status: {
            dirty: row.gitDirty,
            ahead: row.gitAhead,
            behind: row.gitBehind
          },
          chat_bound_thread_count: row.chatCount
        }
      : {};
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

  function openRepoSettings(target: RepoSettingsTarget): void {
    repoSettingsTarget = target;
    repoSettingsOpen = true;
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
