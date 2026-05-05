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
    const [runs, chats] = await Promise.all([pmaApi.ticketFlow.listRuns(), pmaApi.pma.listChats()]);
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
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  selectedWorkspaceFilter="all"
  {sectionIssues}
  onRetry={loadTickets}
  onFilter={(filter) => (selectedFilter = filter)}
  errorMessage={error?.message ?? null}
/>
