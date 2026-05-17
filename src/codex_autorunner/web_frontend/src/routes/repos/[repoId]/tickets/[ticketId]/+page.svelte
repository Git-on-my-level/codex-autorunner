<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy, onMount } from 'svelte';
  import TicketViews from '$lib/components/TicketViews.svelte';
  import { webApi, type ApiError, type JsonRecord, type PartialPageIssue } from '$lib/api/client';
  import { openFlowRunEventSource, type StreamSubscription } from '$lib/api/streaming';
  import {
    invalidateReadModelTags,
    loadScopedTicketDetailSession,
    readModelEntityStore,
    readModelEntityTags,
    renderScopedTicketCachedDetail,
    ticketFlowEventShouldReload
  } from '$lib/data';
  import {
    buildTicketWorkerActivity,
    buildTicketUpdateContent,
    buildTicketRepairChatCreatePayload,
    buildTicketRepairPrompt,
    type TicketDetailViewModel,
    type TicketEditPayload
  } from '$lib/viewModels/ticket';
  import { agentCanListModels, agentId } from '$lib/viewModels/modelPickers';
  import { withRuntimeBasePath as href } from '$lib/runtime/basePath';
  import { buildManagedThreadMessagePayload } from '$lib/viewModels/pmaChat';

  const repoId = $derived(page.params.repoId ?? 'unknown-repo');
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
    const result = await webApi.pma.listAgents();
    if (!result.ok) return;
    agents = result.data.agents;
    const entries = await Promise.all(
      result.data.agents
        .filter((agent) => agentCanListModels(agent))
        .map(async (agent) => {
          const id = agentId(agent);
          const models = await webApi.pma.listAgentModels(id);
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
    if (showLoading) {
      const cached = renderScopedTicketCachedDetail({ kind: 'repo', id: ownerId }, routeTicketId, {
        readModelState
      });
      if (cached) {
        detail = cached.detail;
        loading = false;
      }
    }
    const session = await loadScopedTicketDetailSession(webApi, { kind: 'repo', id: ownerId }, routeTicketId);
    if (!isCurrentRequest()) return;
    if (!session.ok) {
      error = session.error;
      loading = false;
      return;
    }
    currentRunId = session.currentRunId;
    detail = session.detail;
    sectionIssues = session.sectionIssues;
    dispatchHistory = session.dispatches;
    loading = false;
    if (currentRunId) connectFlowStream(currentRunId, ownerId);
  }

  function connectFlowStream(runId: string, ownerId: string): void {
    closeFlowStream();
    streamSubscription = openFlowRunEventSource(runId, { repo: ownerId }, {
      onEvent: (event) => {
        const payload = { ...event.payload, seq: event.payload.seq ?? event.id };
        flowEvents = [...flowEvents, payload].slice(-120);
        if (ticketFlowEventShouldReload(payload)) {
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

  async function runCommand(command: 'resume' | 'bootstrap'): Promise<void> {
    actionStatus = command === 'resume' ? 'Continuing repo ticket flow...' : 'Retrying repo ticket flow...';
    const path =
      command === 'resume' && currentRunId
        ? `/repos/${encodeURIComponent(repoId)}/api/flows/${encodeURIComponent(currentRunId)}/resume`
        : `/repos/${encodeURIComponent(repoId)}/api/flows/ticket_flow/bootstrap`;
    const result = await webApi.requestJson(path, { method: 'POST', body: command === 'bootstrap' ? { once: false } : undefined });
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
    const result = await webApi.ticketFlow.updateTicket(ticketNumber, buildTicketUpdateContent(detail, payload), { repo: repoId });
    saveStatus = result.ok ? 'Ticket saved.' : result.error.message;
    if (result.ok) {
      await invalidateReadModelTags([
        readModelEntityTags.ticket(ticketId),
        readModelEntityTags.ticketIndex,
        readModelEntityTags.repo(repoId)
      ]);
      await loadTicketDetail(false);
    }
    return result.ok;
  }

  async function repairWithPma(ticket: TicketDetailViewModel): Promise<void> {
    actionStatus = 'Creating PMA repair chat...';
    const createResult = await webApi.pma.createChat(buildTicketRepairChatCreatePayload(ticket));
    if (!createResult.ok) {
      actionStatus = createResult.error.message;
      return;
    }
    const sendResult = await webApi.pma.sendMessage(createResult.data.id, buildManagedThreadMessagePayload(buildTicketRepairPrompt(ticket), '', false));
    if (!sendResult.ok) {
      actionStatus = sendResult.error.message;
      return;
    }
    await invalidateReadModelTags([
      readModelEntityTags.chatIndex,
      readModelEntityTags.chat(createResult.data.id),
      readModelEntityTags.ticket(ticketId),
      readModelEntityTags.repo(repoId)
    ]);
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
