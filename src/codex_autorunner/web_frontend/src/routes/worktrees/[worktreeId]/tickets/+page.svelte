<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { confirmDialog } from '$lib/components/confirmDialog';
  import { webApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    invalidateReadModelTags,
    loadScopedTicketListSession,
    readModelEntityStore,
    readModelEntityTags,
    selectTicketListView
  } from '$lib/data';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    reorderScopedTicket,
    runScopedTicketQueueCommand,
    scopedTicketActionStatus,
    scopedTicketQueueScope,
    type ScopedTicketQueueConfig
  } from '$lib/viewModels/scopedTicketQueue';
  import type { SurfaceActionManifest, TicketFilter, TicketListViewModel } from '$lib/viewModels/ticket';

  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  const routeRepoId = $derived(page.params.repoId ?? null);
  let hubParentRepoId = $state<string | null>(null);
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'worktree',
    resourceId: worktreeId,
    // Worktree runtime APIs are mounted as workspace apps under /repos/{workspaceId};
    // /worktrees/{id} is the PMA shell route.
    apiBasePath: `/repos/${encodeURIComponent(worktreeId)}/api/flows`,
    displayLabel: 'worktree',
    parentRepoId: routeRepoId ?? hubParentRepoId
  });
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  let actionManifest = $state<SurfaceActionManifest | null>(null);
  const ownerScope = $derived(scopedTicketQueueScope(queueConfig));
  const list = $derived<TicketListViewModel | null>(selectTicketListView(readModelState, ownerScope, actionManifest));
  let selectedFilter = $state<TicketFilter>('all');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
    void loadTickets();
  });

  onDestroy(() => {
    unsubscribeReadModels?.();
  });

  async function loadTickets(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const session = await loadScopedTicketListSession(webApi, queueConfig, {
      currentPath: stripRuntimeBasePath(page.url.pathname)
    });
    if (!session.ok) {
      error = session.error;
      loading = false;
      return;
    }
    hubParentRepoId = session.parentRepoId;
    if (session.redirectTo) {
      loading = false;
      await goto(href(session.redirectTo), { replaceState: true });
      return;
    }
    actionManifest = session.actionManifest;
    sectionIssues = session.sectionIssues;
    selectedFilter = 'all';
    loading = false;
  }

  async function reorderTicket(sourceRouteId: string, destinationRouteId: string, placeAfter: boolean): Promise<boolean> {
    const result = await reorderScopedTicket(webApi, queueConfig, sourceRouteId, destinationRouteId, placeAfter);
    actionStatus = result.status;
    if (result.ok) {
      await invalidateReadModelTags([
        readModelEntityTags.ticketIndex,
        readModelEntityTags.worktree(worktreeId)
      ]);
      await loadTickets(false);
    }
    return result.ok;
  }

  async function runQueueCommand(command: 'start' | 'stop' | 'restart'): Promise<void> {
    const runId = list?.queueRun?.id ?? null;
    const action = list?.queueActions.find((candidate) => candidate.action === command) ?? null;
    actionStatus = scopedTicketActionStatus(command, queueConfig);
    const result = await runScopedTicketQueueCommand(
      webApi,
      queueConfig,
      command,
      runId,
      () =>
        confirmDialog({
          title: 'Restart ticket flow',
          message: 'This will stop the current run and start a new one.',
          confirmText: 'Restart',
          danger: true
        }),
      action
    );
    actionStatus = result.status;
    if (result.shouldReload) await loadTickets();
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  selectedWorkspaceFilter="all"
  {actionStatus}
  {sectionIssues}
  onRetry={loadTickets}
  onFilter={(filter) => (selectedFilter = filter)}
  onQueueCommand={runQueueCommand}
  onReorderTicket={reorderTicket}
  errorMessage={error?.message ?? null}
/>
