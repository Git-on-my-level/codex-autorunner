<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    pmaChatSummaryToChatIndexRow,
    readModelEntityStore,
    scopedOwnerKey,
    selectTicketListView
  } from '$lib/data';
  import {
    loadScopedActionManifest,
    reorderScopedTicket,
    runScopedTicketQueueCommand,
    scopedTicketActionStatus,
    scopedTicketQueueOwner,
    scopedTicketQueueScope,
    type ScopedTicketQueueConfig
  } from '$lib/viewModels/scopedTicketQueue';
  import type { SurfaceActionManifest } from '$lib/viewModels/ticket';
  import type { TicketFilter, TicketListViewModel } from '$lib/viewModels/ticket';
  import { rememberTickets } from '$lib/viewModels/ticketCache';
  import { mapPmaChatSummary, mapPmaRunProgress, mapTicketSummary } from '$lib/viewModels/domain';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'repo',
    resourceId: repoId,
    apiBasePath: `/repos/${encodeURIComponent(repoId)}/api/flows`,
    displayLabel: 'repo'
  });
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
  let actionManifest = $state<SurfaceActionManifest | null>(null);
  const ownerScope = $derived(scopedTicketQueueScope(queueConfig));
  const ownerKey = $derived(scopedOwnerKey(ownerScope));
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
    const owner = scopedTicketQueueOwner(queueConfig);
    const snapshot = await pmaApi.readModels.repoDetail(repoId);
    if (!snapshot.ok) {
      error = snapshot.error;
      loading = false;
      return;
    }
    const tickets = snapshot.data.scopedTickets.map(mapTicketSummary);
    const runs = snapshot.data.scopedRuns.map(mapPmaRunProgress);
    const chats = snapshot.data.scopedChats.map(mapPmaChatSummary);
    rememberTickets(owner, tickets);
    readModelEntityStore.replaceScopedTicketSummaries(ownerKey, tickets);
    readModelEntityStore.replaceScopedRuns(ownerKey, runs);
    readModelEntityStore.upsertChatIndexRows(chats.map(pmaChatSummaryToChatIndexRow));
    selectedFilter = 'all';
    loading = false;
    const manifest = await loadScopedActionManifest(pmaApi, queueConfig);
    actionManifest = dataOr(manifest, null);
    sectionIssues = [
      !manifest.ok ? partialPageIssue('action_manifest', 'Action manifest unavailable', manifest.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    selectedFilter = 'all';
    loading = false;
  }

  async function reorderTicket(sourceRouteId: string, destinationRouteId: string, placeAfter: boolean): Promise<boolean> {
    const result = await reorderScopedTicket(pmaApi, queueConfig, sourceRouteId, destinationRouteId, placeAfter);
    actionStatus = result.status;
    if (result.ok) await loadTickets(false);
    return result.ok;
  }

  async function runQueueCommand(command: 'start' | 'stop' | 'restart'): Promise<void> {
    const runId = list?.queueRun?.id ?? null;
    const action = list?.queueActions.find((candidate) => candidate.action === command) ?? null;
    actionStatus = scopedTicketActionStatus(command, queueConfig);
    const result = await runScopedTicketQueueCommand(
      pmaApi,
      queueConfig,
      command,
      runId,
      () => window.confirm('Restart ticket flow? This will stop the current run and start a new one.'),
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
