<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildTicketDetailViewModel,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel
  } from '$lib/viewModels/ticket';
  import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary } from '$lib/viewModels/domain';

  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let detail = $state<TicketDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
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
    sectionIssues = [];
    const [tickets, runs, chats] = await Promise.all([
      pmaApi.ticketFlow.listTickets(),
      pmaApi.ticketFlow.listRuns(),
      pmaApi.pma.listChats()
    ]);
    const baseIssues = [
      !tickets.ok ? partialPageIssue('ticket_contract', 'Ticket queue unavailable', tickets.error) : null,
      !runs.ok ? partialPageIssue('timeline', 'Run state unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('linked_chat', 'PMA chats unavailable', chats.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));

    const ticketList = dataOr(tickets, []);
    const selected = tickets.ok ? resolveTicketRouteId(ticketList, ticketId) : null;
    if (!selected) {
      const numericRouteId = Number(ticketId);
      if (!Number.isInteger(numericRouteId) || numericRouteId < 0) {
        error = tickets.ok
          ? {
              kind: 'http',
              status: 404,
              code: 'ticket_not_found',
              message: `Ticket ${ticketId} was not found.`
            }
          : tickets.error;
        loading = false;
        return;
      }
      const directTicket = await pmaApi.ticketFlow.getTicket(numericRouteId);
      if (!directTicket.ok) {
        error = tickets.ok ? directTicket.error : tickets.error;
        loading = false;
        return;
      }
      await renderTicketDetail(directTicket.data, ticketList, dataOr(runs, []), dataOr(chats, []), baseIssues);
      return;
    }

    let ticketDetail: TicketDetail;
    if (selected.number !== null) {
      const ticketResult = await pmaApi.ticketFlow.getTicket(selected.number);
      if (!ticketResult.ok) {
        ticketDetail = ticketDetailFromSummary(selected);
        baseIssues.push(partialPageIssue('ticket_contract', 'Full ticket contract unavailable', ticketResult.error));
      } else {
        ticketDetail = ticketResult.data;
      }
    } else {
      ticketDetail = ticketDetailFromSummary(selected);
    }

    await renderTicketDetail(ticketDetail, ticketList, dataOr(runs, []), dataOr(chats, []), baseIssues);
  }

  async function renderTicketDetail(
    ticketDetail: TicketDetail,
    ticketList: TicketSummary[],
    runs: PmaRunProgress[],
    chats: PmaChatSummary[],
    baseIssues: PartialPageIssue[]
  ): Promise<void> {
    const baseSource = {
      tickets: ticketList,
      runs,
      chats,
      artifacts: [] as SurfaceArtifact[]
    };
    const baseDetail = buildTicketDetailViewModel(ticketDetail, baseSource);
    currentRunId = baseDetail.runHref?.match(/\/api\/flows\/([^/]+)\/status/)?.[1] ?? null;
    const artifactResult = currentRunId ? await pmaApi.ticketFlow.listArtifacts(currentRunId) : null;
    sectionIssues = artifactResult && !artifactResult.ok
      ? [...baseIssues, partialPageIssue('artifacts', 'Surfaced artifacts unavailable', artifactResult.error)]
      : baseIssues;
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
  {sectionIssues}
  onRetry={() => loadTicketDetail()}
  onCommand={runCommand}
  errorMessage={error?.message ?? null}
/>
