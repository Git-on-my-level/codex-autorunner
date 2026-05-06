<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import { buildTicketListViewModel, type TicketFilter, type TicketListViewModel } from '$lib/viewModels/ticket';
  import { rememberTickets } from '$lib/viewModels/ticketCache';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
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
    const tickets = await pmaApi.ticketFlow.listTickets({ repo: repoId });
    if (!tickets.ok) {
      error = tickets.error;
      loading = false;
      return;
    }
    rememberTickets({ repo: repoId }, tickets.data);
    list = buildTicketListViewModel(
      {
        tickets: tickets.data,
        runs: [],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: repoId }
    );
    selectedFilter = 'open';
    loading = false;
    const [runs, chats] = await Promise.all([pmaApi.ticketFlow.listRuns({ repo: repoId }), pmaApi.pma.listChats()]);
    sectionIssues = [
      !runs.ok ? partialPageIssue('timeline', 'Run state unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('linked_chat', 'PMA chats unavailable', chats.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    list = buildTicketListViewModel(
      {
        tickets: tickets.data,
        runs: dataOr(runs, []),
        chats: dataOr(chats, []),
        artifacts: []
      },
      { kind: 'repo', id: repoId }
    );
    selectedFilter = 'open';
    loading = false;
  }

  async function createTicket(payload: { title: string; body: string }): Promise<boolean> {
    actionStatus = 'Creating repo ticket...';
    const result = await pmaApi.ticketFlow.createTicket({ agent: 'codex', title: payload.title, body: payload.body }, { repo: repoId });
    actionStatus = result.ok ? 'Ticket created.' : result.error.message;
    if (result.ok) await loadTickets();
    return result.ok;
  }

  async function reorderTicket(sourceRouteId: string, destinationRouteId: string, placeAfter: boolean): Promise<boolean> {
    const sourceIndex = Number(sourceRouteId);
    const destinationIndex = Number(destinationRouteId);
    if (!Number.isInteger(sourceIndex) || !Number.isInteger(destinationIndex)) {
      actionStatus = 'Only numbered tickets can be reordered.';
      return false;
    }
    actionStatus = 'Reordering repo tickets...';
    const result = await pmaApi.ticketFlow.reorderTicket(sourceIndex, destinationIndex, placeAfter, { repo: repoId });
    actionStatus = result.ok ? 'Ticket order updated.' : result.error.message;
    if (result.ok) await loadTickets();
    return result.ok;
  }

  async function runQueueCommand(command: 'start' | 'stop' | 'restart'): Promise<void> {
    const runId = list?.queueRun?.id ?? null;
    actionStatus = command === 'start' ? 'Starting repo ticket flow...' : command === 'stop' ? 'Stopping repo ticket flow...' : 'Restarting repo ticket flow...';
    if ((command === 'stop' || command === 'restart') && !runId) {
      actionStatus = 'No repo ticket flow run found.';
      return;
    }
    const basePath = `/repos/${encodeURIComponent(repoId)}/api/flows`;
    if (command === 'restart' && !window.confirm('Restart ticket flow? This will stop the current run and start a new one.')) {
      actionStatus = null;
      return;
    }
    const result =
      command === 'stop'
        ? await pmaApi.requestJson(`${basePath}/${encodeURIComponent(runId!)}/stop`, { method: 'POST' })
        : command === 'restart'
          ? await restartQueueRun(basePath, runId!)
          : await pmaApi.requestJson(`${basePath}/ticket_flow/bootstrap`, { method: 'POST', body: {} });
    actionStatus = result.ok ? 'Ticket flow command accepted.' : result.error.message;
    await loadTickets();
  }

  async function restartQueueRun(basePath: string, runId: string) {
    const stopResult = await pmaApi.requestJson(`${basePath}/${encodeURIComponent(runId)}/stop`, { method: 'POST' });
    if (!stopResult.ok) return stopResult;
    return pmaApi.requestJson(`${basePath}/ticket_flow/bootstrap`, {
      method: 'POST',
      body: { metadata: { force_new: true } }
    });
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
