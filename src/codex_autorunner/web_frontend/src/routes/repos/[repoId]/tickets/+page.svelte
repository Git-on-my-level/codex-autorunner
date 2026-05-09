<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    loadScopedTicketQueue,
    reorderScopedTicket,
    runScopedTicketQueueCommand,
    scopedTicketActionStatus,
    type ScopedTicketQueueConfig
  } from '$lib/viewModels/scopedTicketQueue';
  import type { TicketFilter, TicketListViewModel } from '$lib/viewModels/ticket';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const queueConfig = $derived<ScopedTicketQueueConfig>({
    kind: 'repo',
    resourceId: repoId,
    apiBasePath: `/repos/${encodeURIComponent(repoId)}/api/flows`,
    displayLabel: 'repo'
  });
  let list = $state<TicketListViewModel | null>(null);
  let selectedFilter = $state<TicketFilter>('all');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);

  onMount(() => {
    void loadTickets();
  });

  async function loadTickets(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const result = await loadScopedTicketQueue(pmaApi, queueConfig, (initialList) => {
      list = initialList;
      selectedFilter = 'all';
      loading = false;
    });
    if (!result.ok) {
      error = result.error;
      loading = false;
      return;
    }
    list = result.list;
    sectionIssues = result.sectionIssues;
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
