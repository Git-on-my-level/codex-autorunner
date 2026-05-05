<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError } from '$lib/api/client';
  import {
    buildTicketDetailViewModel,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel
  } from '$lib/viewModels/ticket';
  import type { SurfaceArtifact, TicketDetail } from '$lib/viewModels/domain';

  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let detail = $state<TicketDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let actionStatus = $state<string | null>(null);
  let currentRunId = $state<string | null>(null);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    void loadTicketDetail();
    refreshTimer = setInterval(() => void loadTicketDetail(false), 10000);
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
  });

  async function loadTicketDetail(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
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

    const ticketList = tickets.ok ? tickets.data : [];
    const selected = resolveTicketRouteId(ticketList, ticketId);
    if (!selected) {
      error = {
        kind: 'http',
        status: 404,
        code: 'ticket_not_found',
        message: `Ticket ${ticketId} was not found.`
      };
      loading = false;
      return;
    }

    let ticketDetail: TicketDetail;
    if (selected.number !== null) {
      const ticketResult = await pmaApi.ticketFlow.getTicket(selected.number);
      if (!ticketResult.ok) {
        error = ticketResult.error;
        loading = false;
        return;
      }
      ticketDetail = ticketResult.data;
    } else {
      ticketDetail = ticketDetailFromSummary(selected);
    }

    const baseSource = {
      tickets: ticketList,
      runs: runs.ok ? runs.data : [],
      chats: chats.ok ? chats.data : [],
      artifacts: [] as SurfaceArtifact[]
    };
    const baseDetail = buildTicketDetailViewModel(ticketDetail, baseSource);
    currentRunId = baseDetail.runHref?.match(/\/api\/flows\/([^/]+)\/status/)?.[1] ?? null;
    const artifactResult = currentRunId ? await pmaApi.ticketFlow.listArtifacts(currentRunId) : null;
    detail = buildTicketDetailViewModel(ticketDetail, {
      ...baseSource,
      artifacts: artifactResult?.ok ? artifactResult.data : []
    });
    loading = false;
  }

  async function runCommand(command: 'resume' | 'bootstrap'): Promise<void> {
    actionStatus = command === 'resume' ? 'Continuing ticket flow...' : 'Retrying ticket flow...';
    const result =
      command === 'resume' && currentRunId
        ? await pmaApi.ticketFlow.resumeRun(currentRunId)
        : await pmaApi.ticketFlow.bootstrap();
    actionStatus = result.ok ? 'Ticket flow command accepted.' : result.error.message;
    await loadTicketDetail(false);
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {actionStatus}
  onCommand={runCommand}
  errorMessage={error?.message ?? null}
/>
