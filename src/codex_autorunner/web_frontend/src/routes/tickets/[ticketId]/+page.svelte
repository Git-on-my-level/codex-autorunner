<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildTicketDetailViewModel,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel
  } from '$lib/viewModels/ticket';
  import type { TicketSummary } from '$lib/viewModels/domain';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';

  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let detail = $state<TicketDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);

  onMount(() => {
    void loadTicket();
  });

  async function loadTicket(): Promise<void> {
    loading = true;
    error = null;
    const result = await pmaApi.ticketFlow.listTickets();
    if (!result.ok) {
      error = result.error;
      detail = null;
      loading = false;
      return;
    }
    const tickets = result.data;
    const selected = resolveTicketRouteId(tickets, ticketId);
    if (!selected) {
      error = {
        kind: 'http',
        status: 404,
        code: 'ticket_not_found',
        message: `Ticket not found: ${ticketId}`
      };
      detail = null;
      loading = false;
      return;
    }
    const canonical = canonicalTicketHref(selected);
    if (canonical) {
      await goto(href(canonical), { replaceState: true });
      return;
    }
    detail = buildTicketDetailViewModel(ticketDetailFromSummary(selected), {
      tickets,
      runs: [],
      chats: [],
      artifacts: []
    });
    loading = false;
  }

  function canonicalTicketHref(ticket: TicketSummary): string | null {
    const routeId = encodeURIComponent(ticket.number != null ? String(ticket.number) : ticket.id);
    if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
      return `/repos/${encodeURIComponent(ticket.workspaceId)}/tickets/${routeId}`;
    }
    if (ticket.workspaceKind === 'worktree' && ticket.workspaceId && ticket.repoId) {
      return `/repos/${encodeURIComponent(ticket.repoId)}/worktrees/${encodeURIComponent(ticket.workspaceId)}/tickets/${routeId}`;
    }
    return null;
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  onRetry={loadTicket}
  errorMessage={error?.message ?? null}
/>
