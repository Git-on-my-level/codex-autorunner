<script lang="ts">
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import { buildTicketListViewModel, type TicketFilter, type TicketListViewModel } from '$lib/viewModels/ticket';

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
