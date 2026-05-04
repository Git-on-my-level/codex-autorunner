<script lang="ts">
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildTicketListViewModel,
    type TicketFilter,
    type TicketListViewModel
  } from '$lib/viewModels/ticket';

  let list = $state<TicketListViewModel | null>(null);
  let selectedFilter = $state<TicketFilter>('needs_attention');
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadTickets();
  });

  async function loadTickets(): Promise<void> {
    loading = true;
    error = null;
    const [tickets, runs, chats] = await Promise.all([
      pmaApi.ticketFlow.listTickets(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats()
    ]);
    const firstError = [tickets, runs, chats].find((result) => !result.ok);
    if (firstError && !firstError.ok) {
      error = firstError.error;
      loading = false;
      return;
    }
    list = buildTicketListViewModel({
      tickets: tickets.ok ? tickets.data : [],
      runs: runs.ok ? runs.data : [],
      chats: chats.ok ? chats.data : [],
      artifacts: []
    });
    selectedFilter = list.defaultFilter;
    loading = false;
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  onFilter={(filter) => (selectedFilter = filter)}
  errorMessage={error?.message ?? null}
/>
