<script lang="ts">
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type PartialPageIssue } from '$lib/api/client';
  import {
    buildTicketUpdateContent,
    buildTicketDetailViewModel,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel,
    type TicketEditPayload
  } from '$lib/viewModels/ticket';
  import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary } from '$lib/viewModels/domain';
  import { cachedTickets, rememberTickets } from '$lib/viewModels/ticketCache';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let detail = $state<TicketDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);
  let saveStatus = $state<string | null>(null);
  let currentRunId = $state<string | null>(null);
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  // SvelteKit reuses this page while only route params change; slow refreshes must not repaint a previous ticket.
  let detailRequestSeq = 0;

  onMount(() => {
    refreshTimer = setInterval(() => void loadTicketDetail(false), 10000);
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
  });

  $effect(() => {
    const ownerId = repoId;
    const routeTicketId = ticketId;
    actionStatus = null;
    saveStatus = null;
    void loadTicketDetail(true, ownerId, routeTicketId);
  });

  async function loadTicketDetail(
    showLoading = true,
    ownerId = repoId,
    routeTicketId = ticketId
  ): Promise<void> {
    const requestSeq = ++detailRequestSeq;
    const isCurrentRequest = () => requestSeq === detailRequestSeq && ownerId === repoId && routeTicketId === ticketId;
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const cachedList = cachedTickets({ repo: ownerId });
    if (showLoading && cachedList) renderCachedTicket(cachedList, ownerId, routeTicketId);
    const tickets = await pmaApi.ticketFlow.listTickets({ repo: ownerId });
    if (!isCurrentRequest()) return;
    const ticketList = dataOr(tickets, []);
    if (tickets.ok) rememberTickets({ repo: ownerId }, ticketList);
    const selected = tickets.ok ? resolveTicketRouteId(ticketList, routeTicketId) : null;
    if (!selected) {
      error = tickets.ok
        ? { kind: 'http', status: 404, code: 'ticket_not_found', message: `Ticket ${routeTicketId} was not found in repo ${ownerId}.` }
        : tickets.error;
      loading = false;
      return;
    }
    const ticketDetail = ticketDetailFromSummary(selected);
    detail = buildTicketDetailViewModel(ticketDetail, { tickets: ticketList, runs: [], chats: [], artifacts: [] });
    sectionIssues = [];
    loading = false;
    const [runs, chats] = await Promise.all([pmaApi.ticketFlow.listRuns({ repo: ownerId }), pmaApi.pma.listChats()]);
    const baseIssues = [
      !runs.ok ? partialPageIssue('timeline', 'Run state unavailable', runs.error) : null,
      !chats.ok ? partialPageIssue('linked_chat', 'PMA chats unavailable', chats.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    if (!isCurrentRequest()) return;
    await renderTicketDetail(ticketDetail, ticketList, dataOr(runs, []), dataOr(chats, []), baseIssues, ownerId, isCurrentRequest);
  }

  function renderCachedTicket(ticketList: TicketSummary[], ownerId: string, routeTicketId: string): void {
    if (ownerId !== repoId || routeTicketId !== ticketId) return;
    const selected = resolveTicketRouteId(ticketList, routeTicketId);
    if (!selected) return;
    detail = buildTicketDetailViewModel(ticketDetailFromSummary(selected), {
      tickets: ticketList,
      runs: [],
      chats: [],
      artifacts: []
    });
    loading = false;
  }

  async function renderTicketDetail(
    ticketDetail: TicketDetail,
    ticketList: TicketSummary[],
    runs: PmaRunProgress[],
    chats: PmaChatSummary[],
    baseIssues: PartialPageIssue[],
    ownerId: string,
    isCurrentRequest = () => true
  ): Promise<void> {
    if (!isCurrentRequest()) return;
    const baseSource = { tickets: ticketList, runs, chats, artifacts: [] as SurfaceArtifact[] };
    const baseDetail = buildTicketDetailViewModel(ticketDetail, baseSource);
    currentRunId = baseDetail.runHref?.match(/\/api\/flows\/([^/]+)\/status/)?.[1] ?? null;
    detail = baseDetail;
    sectionIssues = baseIssues;
    loading = false;
    const artifactResult = currentRunId ? await pmaApi.ticketFlow.listArtifacts(currentRunId, { repo: ownerId }) : null;
    if (!isCurrentRequest()) return;
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
    actionStatus = command === 'resume' ? 'Continuing repo ticket flow...' : 'Retrying repo ticket flow...';
    const path =
      command === 'resume' && currentRunId
        ? `/repos/${encodeURIComponent(repoId)}/api/flows/${encodeURIComponent(currentRunId)}/resume`
        : `/repos/${encodeURIComponent(repoId)}/api/flows/ticket_flow/bootstrap`;
    const result = await pmaApi.requestJson(path, { method: 'POST', body: command === 'bootstrap' ? { once: false } : undefined });
    actionStatus = result.ok ? 'Ticket flow command accepted.' : result.error.message;
    await loadTicketDetail(false);
  }

  async function saveTicket(payload: TicketEditPayload): Promise<boolean> {
    if (!detail) return false;
    const ticketNumber = Number(detail.routeId);
    if (!Number.isInteger(ticketNumber)) {
      saveStatus = 'This ticket cannot be edited until it has a numeric TICKET index.';
      return false;
    }
    saveStatus = 'Saving ticket...';
    const result = await pmaApi.ticketFlow.updateTicket(ticketNumber, buildTicketUpdateContent(detail, payload), { repo: repoId });
    saveStatus = result.ok ? 'Ticket saved.' : result.error.message;
    if (result.ok) await loadTicketDetail(false);
    return result.ok;
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {actionStatus}
  {saveStatus}
  {sectionIssues}
  onRetry={() => loadTicketDetail()}
  onCommand={runCommand}
  onSave={saveTicket}
  errorMessage={error?.message ?? null}
/>
