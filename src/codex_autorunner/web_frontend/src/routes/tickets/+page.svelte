<script lang="ts">
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { readModelEntityStore } from '$lib/data';
  import { buildTicketListViewModel, type TicketFilter, type TicketListViewModel } from '$lib/viewModels/ticket';
  import { mapTicketSummary } from '$lib/viewModels/domain';

  let list = $state<TicketListViewModel | null>(null);
  let selectedFilter = $state<TicketFilter>('all');
  let selectedWorkspaceFilter = $state('all');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadTickets();
  });

  async function loadTickets(): Promise<void> {
    loading = true;
    error = null;

    const state = readModelEntityStore.snapshot();
    const cachedIds = state.ticketOrderByOwner['all'];
    if (cachedIds?.length) {
      const cached = cachedIds.map(id => state.ticketSummaries[id]).filter(Boolean);
      if (cached.length > 0) {
        list = buildTicketListViewModel({ tickets: cached, runs: [], chats: [], artifacts: [] });
        selectedFilter = 'all';
        selectedWorkspaceFilter = 'all';
        loading = false;
        return;
      }
    }

    const result = await pmaApi.ticketFlow.listTickets();
    if (!result.ok) {
      error = result.error;
      list = null;
      loading = false;
      return;
    }
    list = buildTicketListViewModel({
      tickets: result.data,
      runs: [],
      chats: [],
      artifacts: []
    });
    selectedFilter = 'all';
    selectedWorkspaceFilter = 'all';
    loading = false;
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  {selectedWorkspaceFilter}
  onRetry={loadTickets}
  onFilter={(filter) => (selectedFilter = filter)}
  errorMessage={error?.message ?? null}
/>
