<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { pmaApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import { openFlowRunEventSource, type StreamSubscription } from '$lib/api/streaming';
  import {
    pmaChatSummaryToChatIndexRow,
    readModelEntityStore,
    scopedOwnerKey,
    selectPmaChats,
    selectPmaRuns,
    selectTicketSummaries
  } from '$lib/data';
  import { stripRuntimeBasePath, withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import {
    buildTicketWorkerActivity,
    buildTicketUpdateContent,
    buildTicketDetailViewModel,
    buildTicketRepairChatCreatePayload,
    buildTicketRepairPrompt,
    resolveTicketRouteId,
    ticketDetailFromSummary,
    type TicketDetailViewModel,
    type TicketEditPayload
  } from '$lib/viewModels/ticket';
  import { legacyWorktreeRedirectPath } from '$lib/viewModels/routes';
  import {
    mapPmaChatSummary,
    mapPmaRunProgress,
    mapTicketDetail,
    mapTicketSummary,
    type PmaChatSummary,
    type PmaRunProgress,
    type SurfaceArtifact,
    type TicketDetail,
    type TicketSummary
  } from '$lib/viewModels/domain';
  import { cachedTickets, rememberTickets } from '$lib/viewModels/ticketCache';
  import { agentCanListModels, agentId } from '$lib/viewModels/modelPickers';
  import { buildManagedThreadMessagePayload } from '$lib/viewModels/pmaChat';

  const worktreeId = $derived(page.params.worktreeId ?? 'unknown-worktree');
  const ticketId = $derived(page.params.ticketId ?? 'unknown-ticket');
  let readModelState = $state(readModelEntityStore.snapshot());
  let unsubscribeReadModels: (() => void) | null = null;
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
  let agents = $state<JsonRecord[]>([]);
  let modelCatalogs = $state<Record<string, JsonRecord[] | null>>({});
  // SvelteKit reuses this page while only route params change; slow refreshes must not repaint a previous ticket.
  let detailRequestSeq = 0;

  onMount(() => {
    unsubscribeReadModels = readModelEntityStore.subscribe((state) => {
      readModelState = state;
    });
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
    unsubscribeReadModels?.();
    closeFlowStream();
  });

  $effect(() => {
    const ownerId = worktreeId;
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
    ownerId = worktreeId,
    routeTicketId = ticketId
  ): Promise<void> {
    const requestSeq = ++detailRequestSeq;
    const isCurrentRequest = () => requestSeq === detailRequestSeq && ownerId === worktreeId && routeTicketId === ticketId;
    if (showLoading) loading = true;
    error = null;
    sectionIssues = [];
    const cachedList = cachedTickets({ worktree: ownerId });
    if (showLoading && cachedList) renderCachedTicket(cachedList, ownerId, routeTicketId);
    const snapshot = await pmaApi.readModels.ticketDetail(routeTicketId, { kind: 'worktree', id: ownerId });
    if (!isCurrentRequest()) return;
    if (!snapshot.ok) {
      error = snapshot.error;
      loading = false;
      return;
    }
    const legacyTicket = (snapshot.data.legacyTicket ?? {}) as JsonRecord;
    const parentRepoId = typeof legacyTicket.base_repo_id === 'string' ? legacyTicket.base_repo_id : null;
    const redirectTo = legacyWorktreeRedirectPath(stripRuntimeBasePath(page.url.pathname), ownerId, parentRepoId);
    if (redirectTo) {
      await goto(href(redirectTo), { replaceState: true });
      return;
    }
    const loadedTicketList = (snapshot.data.scopedTickets ?? []).map(mapTicketSummary);
    const loadedRuns = (snapshot.data.scopedRuns ?? []).map(mapPmaRunProgress);
    const loadedChats = (snapshot.data.scopedChats ?? []).map(mapPmaChatSummary);
    const ownerKey = scopedOwnerKey({ kind: 'worktree', id: ownerId, parentRepoId });
    rememberTickets({ worktree: ownerId }, loadedTicketList);
    readModelEntityStore.replaceScopedTicketSummaries(ownerKey, loadedTicketList);
    readModelEntityStore.replaceScopedRuns(ownerKey, loadedRuns);
    readModelEntityStore.upsertChatIndexRows(loadedChats.map(pmaChatSummaryToChatIndexRow));
    const ticketList = selectTicketSummaries(readModelState, ownerKey);
    const ticketDetail = mapTicketDetail(legacyTicket);
    detail = buildTicketDetailViewModel(ticketDetail, { tickets: ticketList, runs: [], chats: [], artifacts: [] });
    sectionIssues = [];
    loading = false;
    const runs = selectPmaRuns(readModelState, ownerKey);
    const chats = selectPmaChats(readModelState);
    if (!isCurrentRequest()) return;
    renderTicketDetail(ticketDetail, ticketList, runs, chats, snapshot.data.dispatches ?? [], [], ownerId, isCurrentRequest);
  }

  function renderCachedTicket(ticketList: TicketSummary[], ownerId: string, routeTicketId: string): void {
    if (ownerId !== worktreeId || routeTicketId !== ticketId) return;
    const selected = resolveTicketRouteId(ticketList, routeTicketId);
    if (!selected) return;
    readModelEntityStore.replaceScopedTicketSummaries(scopedOwnerKey({ kind: 'worktree', id: ownerId }), ticketList);
    detail = buildTicketDetailViewModel(ticketDetailFromSummary(selected), {
      tickets: ticketList,
      runs: [],
      chats: [],
      artifacts: []
    });
    loading = false;
  }

  function renderTicketDetail(
    ticketDetail: TicketDetail,
    ticketList: TicketSummary[],
    runs: PmaRunProgress[],
    chats: PmaChatSummary[],
    dispatches: JsonRecord[],
    baseIssues: PartialPageIssue[],
    ownerId: string,
    isCurrentRequest = () => true
  ): void {
    if (!isCurrentRequest()) return;
    const baseSource = { tickets: ticketList, runs, chats, artifacts: [] as SurfaceArtifact[] };
    const baseDetail = buildTicketDetailViewModel(ticketDetail, baseSource);
    currentRunId = baseDetail.flowRunId;
    detail = baseDetail;
    sectionIssues = baseIssues;
    loading = false;
    dispatchHistory = dispatches;
    if (currentRunId) connectFlowStream(currentRunId, ownerId);
    detail = buildTicketDetailViewModel(ticketDetail, {
      ...baseSource,
      artifacts: []
    });
    loading = false;
  }

  function connectFlowStream(runId: string, ownerId: string): void {
    closeFlowStream();
    streamSubscription = openFlowRunEventSource(runId, { worktree: ownerId }, {
      onEvent: (event) => {
        const payload = { ...event.payload, seq: event.payload.seq ?? event.id };
        flowEvents = [...flowEvents, payload].slice(-120);
        if (isTerminalFlowEvent(payload)) {
          void loadTicketDetail(false, ownerId, ticketId);
          closeFlowStream();
        }
      },
      onError: () => {
        void loadTicketDetail(false, ownerId, ticketId);
      }
    });
  }

  function closeFlowStream(): void {
    streamSubscription?.close();
    streamSubscription = null;
  }

  function isTerminalFlowEvent(payload: JsonRecord): boolean {
    const status = String(payload.status ?? payload.flow_status ?? payload.state ?? '').toLowerCase();
    const eventType = String(payload.event_type ?? payload.type ?? '').toLowerCase();
    return ['completed', 'complete', 'done', 'failed', 'cancelled', 'canceled'].includes(status) || eventType.includes('terminal');
  }

  async function runCommand(command: 'resume' | 'bootstrap'): Promise<void> {
    actionStatus = command === 'resume' ? 'Continuing worktree ticket flow...' : 'Retrying worktree ticket flow...';
    // Worktree runtime APIs are still mounted as workspace apps under
    // /repos/{workspaceId}; /worktrees/{id} is the PMA shell route.
    const path =
      command === 'resume' && currentRunId
        ? `/repos/${encodeURIComponent(worktreeId)}/api/flows/${encodeURIComponent(currentRunId)}/resume`
        : `/repos/${encodeURIComponent(worktreeId)}/api/flows/ticket_flow/bootstrap`;
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
    const result = await pmaApi.ticketFlow.updateTicket(ticketNumber, buildTicketUpdateContent(detail, payload), { worktree: worktreeId });
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
