<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { dataOr, partialPageIssue, pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import { openFlowRunEventSource, type StreamSubscription } from '$lib/api/streaming';
  import {
    buildTicketWorkerActivity,
    buildTicketUpdateContent,
    buildTicketDetailViewModel,
    buildTicketRepairChatCreatePayload,
    buildTicketRepairPrompt,
    mergeTicketRunProgress,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel,
    type TicketEditPayload
  } from '$lib/viewModels/ticket';
  import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact, TicketDetail, TicketSummary } from '$lib/viewModels/domain';
  import { cachedTickets, rememberTickets } from '$lib/viewModels/ticketCache';
  import { agentCanListModels, agentId } from '$lib/viewModels/modelPickers';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { buildManagedThreadMessagePayload } from '$lib/viewModels/pmaChat';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let detail = $state<TicketDetailViewModel | null>(null);
  let loading = $state(true);
  let error = $state<ApiError | null>(null);
  let sectionIssues = $state<PartialPageIssue[]>([]);
  let actionStatus = $state<string | null>(null);
  let saveStatus = $state<string | null>(null);
  let currentRunId = $state<string | null>(null);
  let dispatchHistory = $state<JsonRecord[]>([]);
  let flowEvents = $state<JsonRecord[]>([]);
  let workerActivity = $derived(buildTicketWorkerActivity(dispatchHistory, flowEvents));
  let streamSubscription: StreamSubscription | null = null;
  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let agents = $state<JsonRecord[]>([]);
  let modelCatalogs = $state<Record<string, JsonRecord[] | null>>({});
  // SvelteKit reuses this page while only route params change; slow refreshes must not repaint a previous ticket.
  let detailRequestSeq = 0;

  onMount(() => {
    refreshTimer = setInterval(() => void loadTicketDetail(false), 10000);
    void loadPickerSupport();
  });

  async function loadPickerSupport(): Promise<void> {
    const result = await pmaApi.pma.listAgents();
    if (!result.ok) return;
    agents = result.data.agents;
    const entries = await Promise.all(
      result.data.agents
        .filter((agent) => agentCanListModels(agent))
        .map(async (agent) => {
          const id = agentId(agent);
          const models = await pmaApi.pma.listAgentModels(id);
          return [id, models.ok ? models.data : null] as const;
        })
    );
    modelCatalogs = Object.fromEntries(entries);
  }

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
    closeFlowStream();
  });

  $effect(() => {
    const ownerId = repoId;
    const routeTicketId = ticketId;
    actionStatus = null;
    saveStatus = null;
    dispatchHistory = [];
    flowEvents = [];
    closeFlowStream();
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
      !chats.ok ? partialPageIssue('linked_chat', 'Chats unavailable', chats.error) : null
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
    currentRunId = baseDetail.flowRunId;
    detail = baseDetail;
    sectionIssues = baseIssues;
    loading = false;
    const [dispatchResult, timelineResult, tailResult, statusResult] = await Promise.all([
      currentRunId ? pmaApi.ticketFlow.getDispatchHistory(currentRunId, { repo: ownerId }) : Promise.resolve(null),
      baseDetail.linkedChatId ? pmaApi.pma.getTimeline(baseDetail.linkedChatId) : Promise.resolve(null),
      baseDetail.linkedChatId ? pmaApi.pma.getTail(baseDetail.linkedChatId) : Promise.resolve(null),
      baseDetail.linkedChatId ? pmaApi.pma.getStatus(baseDetail.linkedChatId) : Promise.resolve(null)
    ]);
    if (!isCurrentRequest()) return;
    sectionIssues = [
      ...baseIssues,
      dispatchResult && !dispatchResult.ok ? partialPageIssue('timeline', 'Worker output unavailable', dispatchResult.error) : null,
      timelineResult && !timelineResult.ok ? partialPageIssue('linked_chat', 'Ticket chat history unavailable', timelineResult.error) : null
    ].filter((issue): issue is PartialPageIssue => Boolean(issue));
    dispatchHistory = dispatchResult?.ok ? dispatchResult.data : [];
    if (currentRunId) connectFlowStream(currentRunId, ownerId);
    const latestProgress = tailResult?.ok ? tailResult.data : statusResult?.ok ? statusResult.data : null;
    detail = buildTicketDetailViewModel(ticketDetail, {
      ...baseSource,
      runs: mergeTicketRunProgress(runs, latestProgress),
      artifacts: [],
      timeline: timelineResult?.ok ? timelineResult.data : []
    });
    loading = false;
  }

  function connectFlowStream(runId: string, ownerId: string): void {
    closeFlowStream();
    streamSubscription = openFlowRunEventSource(runId, { repo: ownerId }, {
      onEvent: (event) => {
        flowEvents = [...flowEvents, { ...event.payload, seq: event.payload.seq ?? event.id }].slice(-120);
      },
      onError: () => closeFlowStream()
    });
  }

  function closeFlowStream(): void {
    streamSubscription?.close();
    streamSubscription = null;
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

  async function repairWithPma(ticket: TicketDetailViewModel): Promise<void> {
    actionStatus = 'Creating PMA repair chat...';
    const createResult = await pmaApi.pma.createChat(buildTicketRepairChatCreatePayload(ticket));
    if (!createResult.ok) {
      actionStatus = createResult.error.message;
      return;
    }
    const sendResult = await pmaApi.pma.sendMessage(createResult.data.id, buildManagedThreadMessagePayload(buildTicketRepairPrompt(ticket), '', false));
    if (!sendResult.ok) {
      actionStatus = sendResult.error.message;
      return;
    }
    await goto(href(`/chats?chat=${encodeURIComponent(createResult.data.id)}`));
  }
</script>

<TicketViews
  state={loading ? 'loading' : error ? 'error' : 'ready'}
  mode="detail"
  {detail}
  {agents}
  {modelCatalogs}
  {actionStatus}
  {saveStatus}
  {workerActivity}
  {sectionIssues}
  onRetry={() => loadTicketDetail()}
  onCommand={runCommand}
  onRepairWithPma={repairWithPma}
  onSave={saveTicket}
  errorMessage={error?.message ?? null}
/>
