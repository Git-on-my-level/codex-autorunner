<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    createScopedTicket,
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
  let selectedFilter = $state<TicketFilter>('open');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);

  onMount(() => {
    void loadTickets();
  });

  async function loadTickets(): Promise<void> {
    loading = true;
    error = null;
    sectionIssues = [];
    const result = await loadScopedTicketQueue(pmaApi, queueConfig, (initialList) => {
      list = initialList;
      selectedFilter = 'open';
      loading = false;
    });
    if (!result.ok) {
      error = result.error;
      loading = false;
      return;
    }
    list = result.list;
    sectionIssues = result.sectionIssues;
    selectedFilter = 'open';
    loading = false;
  }

  async function createTicket(payload: { title: string; body: string }): Promise<boolean> {
    actionStatus = scopedTicketActionStatus('create', queueConfig);
    const result = await createScopedTicket(pmaApi, queueConfig, payload);
    actionStatus = result.status;
    if (result.ok) await loadTickets();
    return result.ok;
  }

  async function reorderTicket(sourceRouteId: string, destinationRouteId: string, placeAfter: boolean): Promise<boolean> {
    actionStatus = scopedTicketActionStatus('reorder', queueConfig);
    const result = await reorderScopedTicket(pmaApi, queueConfig, sourceRouteId, destinationRouteId, placeAfter);
    actionStatus = result.status;
    if (result.ok) await loadTickets();
    return result.ok;
  }

  async function runQueueCommand(command: 'start' | 'stop' | 'restart'): Promise<void> {
    const runId = list?.queueRun?.id ?? null;
    actionStatus = scopedTicketActionStatus(command, queueConfig);
    const result = await runScopedTicketQueueCommand(pmaApi, queueConfig, command, runId, () =>
      window.confirm('Restart ticket flow? This will stop the current run and start a new one.')
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
  onCreateTicket={createTicket}
  onReorderTicket={reorderTicket}
  errorMessage={error?.message ?? null}
/>
