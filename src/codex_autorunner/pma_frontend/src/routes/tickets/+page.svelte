<script lang="ts">
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildTicketListViewModel,
    type TicketFilter,
    type TicketListViewModel
  } from '$lib/viewModels/ticket';

  let list = $state<TicketListViewModel | null>(null);
  let selectedFilter = $state<TicketFilter>('needs_attention');
  let selectedWorkspaceFilter = $state('all');
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
    const [tickets, runs, chats] = await Promise.all([
      pmaApi.ticketFlow.listTickets(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats()
    ]);
    if (!tickets.ok) {
      error = tickets.error;
      loading = false;
      return;
    }
    sectionIssues = [
      !runs.ok ? partialPageIssue('timeline', 'Run state unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('linked_chat', 'PMA chats unavailable', chats.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    list = buildTicketListViewModel({
      tickets: tickets.data,
      runs: dataOr(runs, []),
      chats: dataOr(chats, []),
      artifacts: []
    });
    selectedFilter = list.defaultFilter;
    selectedWorkspaceFilter = workspaceFilterFromUrl() ?? list.defaultWorkspaceFilter;
    loading = false;
  }

  function workspaceFilterFromUrl(): string | null {
    const params = new URL(window.location.href).searchParams;
    const repo = params.get('repo');
    const worktree = params.get('worktree');
    if (worktree) return `worktree:${worktree}`;
    if (repo) return `repo:${repo}`;
    if (params.has('unscoped')) return 'unscoped';
    return null;
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="list"
  {list}
  {selectedFilter}
  {selectedWorkspaceFilter}
  {sectionIssues}
  onRetry={loadTickets}
  onFilter={(filter) => (selectedFilter = filter)}
  errorMessage={error?.message ?? null}
/>
