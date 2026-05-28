<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import AutoDismissNotice from '$lib/components/AutoDismissNotice.svelte';
  import RepoWorktreeViews from '$lib/components/RepoWorktreeViews.svelte';
  import NewRepoDialog from '$lib/components/NewRepoDialog.svelte';
  import NewWorktreeDialog from '$lib/components/NewWorktreeDialog.svelte';
  import RepoSettingsDialog, { type RepoSettingsTarget } from '$lib/components/RepoSettingsDialog.svelte';
  import { confirmAndRetireState, confirmAndRetireWorktree, type ActionNotice } from '$lib/actions/repoWorktreeActions';
  import { webApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    ensureChatIndexLoaded,
    ensureRepoWorktreeIndexLoaded,
    ensureTicketIndexLoaded,
    invalidateReadModelTags,
    readModelEntityStore,
    readModelEntityTags,
    selectChats,
    selectRepoSummaries,
    selectTicketSummaries,
    selectWorktreeSummaries
  } from '$lib/data';
  import {
    buildRepoWorktreeIndexViewModel,
    type RepoWorktreeIndexViewModel
  } from '$lib/viewModels/repoWorktree';

  let { data = { status: 'cold' as const, tags: [] } } = $props();
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  let ticketsListLoaded = $state(false);
  const index = $derived<RepoWorktreeIndexViewModel | null>(
    buildRepoWorktreeIndexViewModel({
      repos: selectRepoSummaries(readModelState),
      worktrees: selectWorktreeSummaries(readModelState),
      runs: [],
      chats: selectChats(readModelState),
      tickets: selectTicketSummaries(readModelState, 'all'),
      artifacts: [],
      ticketsListLoaded
    })
  );
  const hasCachedRows = $derived(Boolean(index && index.rows.length > 0));
  let refreshing = $state<boolean>(false);
  let coldHydrating = $state<boolean>(true);
  let refreshError = $state<ApiError | null>(null);
  const loading = $derived((data.status === 'cold' && coldHydrating && !hasCachedRows) || (refreshing && !hasCachedRows));
  const error = $derived(!hasCachedRows ? refreshError ?? (data.status === 'error' ? data.error : null) : null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let notice = $state<ActionNotice | null>(null);

  let newRepoOpen = $state(false);
  let newWorktreeOpen = $state(false);
  let newWorktreeTarget = $state<{ id: string; label: string } | null>(null);
  let repoSettingsOpen = $state(false);
  let repoSettingsTarget = $state<RepoSettingsTarget | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadRepos({ showLoading: data.status === 'cold' && !hasCachedRows });
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadRepos(options: { showLoading?: boolean } = {}): Promise<void> {
    if (options.showLoading !== false) refreshing = true;
    refreshError = null;
    sectionIssues = [];
    try {
      const [topoResult, chatResult, ticketResult] = await Promise.all([
        ensureRepoWorktreeIndexLoaded({ refresh: true }),
        ensureChatIndexLoaded({}, { refresh: true }),
        ensureTicketIndexLoaded({ refresh: true })
      ]);
      if (topoResult.status === 'error') {
        refreshError = topoResult.error;
      } else if (chatResult.status === 'error') {
        refreshError = chatResult.error;
      } else if (ticketResult.status === 'error') {
        refreshError = ticketResult.error;
      } else {
        ticketsListLoaded = true;
      }
    } finally {
      coldHydrating = false;
      refreshing = false;
    }
  }

  async function handleRetireWorktree(target: Parameters<typeof confirmAndRetireWorktree>[0]): Promise<void> {
    const result = await confirmAndRetireWorktree(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepos();
  }

  async function handleRetireState(target: Parameters<typeof confirmAndRetireState>[0]): Promise<void> {
    const result = await confirmAndRetireState(target);
    if (!result) return;
    notice = result;
    if (result.tone === 'success') await loadRepos();
  }

  async function handleRepoPin(target: { id: string; pinned: boolean }): Promise<void> {
    const reconciliationId = `repo-pin:${target.id}:${target.pinned ? 'pin' : 'unpin'}:${Date.now()}`;
    readModelEntityStore.optimisticRepoPin(target.id, target.pinned, reconciliationId);
    const result = await webApi.hub.setRepoPinned(target.id, target.pinned);
    if (!result.ok) {
      readModelEntityStore.revertOptimisticMutation(reconciliationId);
      notice = { tone: 'danger', message: result.error.message };
      return;
    }
    readModelEntityStore.reconcileOptimisticMutation(reconciliationId);
    await invalidateReadModelTags([
      readModelEntityTags.repoWorktreeIndex,
      readModelEntityTags.repo(target.id)
    ]);
    await loadRepos();
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
    if (result.tone !== 'success') return;
    await loadRepos();
    if (result.navigateTo) void goto(result.navigateTo);
  }
</script>

<AutoDismissNotice message={notice?.message ?? null} tone={notice?.tone ?? 'neutral'} />
<RepoWorktreeViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="index"
  {index}
  {sectionIssues}
  onRetry={loadRepos}
  onRetireWorktree={handleRetireWorktree}
  onRetireState={handleRetireState}
  onRepoPin={handleRepoPin}
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
