import type { ApiError, ApiResult, JsonRecord, PartialPageIssue, WebApiClient } from '$lib/api/client';
import type { FlowRunStreamEvent, StreamSubscription } from '$lib/api/streaming';
import {
  createAgentModelCatalogStore,
  type AgentModelCatalogState,
  type AgentModelCatalogStore
} from '$lib/application/agentModelCatalogStore';
import {
  invalidateReadModelTags,
  loadScopedTicketDetailSession,
  readModelEntityStore,
  readModelEntityTags,
  renderScopedTicketCachedDetail,
  ticketFlowEventShouldReload,
  type ReadModelDependency,
  type ReadModelEntityState,
  type ReadModelEntityStore,
  type ScopedTicketSessionApi
} from '$lib/data';
import { buildManagedThreadMessagePayload } from '$lib/viewModels/chat';
import {
  buildTicketRepairChatCreatePayload,
  buildTicketRepairPrompt,
  buildTicketUpdateContent,
  buildTicketWorkerActivity,
  type TicketDetailViewModel,
  type TicketEditPayload,
  type TicketOwnerScope,
  type TicketWorkerActivity
} from '$lib/viewModels/ticket';
import { chatRoute } from '$lib/viewModels/routes';

export type ScopedTicketDetailOwnerKind = 'repo' | 'worktree';

export type ScopedTicketDetailOwnerScope = Exclude<TicketOwnerScope, null>;

export type ScopedTicketDetailRoute = {
  ownerScope: ScopedTicketDetailOwnerScope;
  ticketId: string;
  currentPath?: string | null;
};

export type ScopedTicketDetailControllerState = {
  ownerScope: ScopedTicketDetailOwnerScope;
  ticketId: string;
  detail: TicketDetailViewModel | null;
  loading: boolean;
  error: ApiError | null;
  sectionIssues: PartialPageIssue[];
  actionStatus: string | null;
  saveStatus: string | null;
  currentRunId: string | null;
  dispatchHistory: JsonRecord[];
  flowEvents: JsonRecord[];
  workerActivity: TicketWorkerActivity;
  agents: JsonRecord[];
  modelCatalogs: Record<string, JsonRecord[] | null>;
};

export type ScopedTicketDetailApi = ScopedTicketSessionApi & {
  requestJson<T>(path: string, options?: unknown): Promise<ApiResult<T>>;
  ticketFlow: Pick<WebApiClient['ticketFlow'], 'updateTicket'>;
  pma: Pick<WebApiClient['pma'], 'listAgents' | 'listAgentModels' | 'createChat' | 'sendMessage'>;
};

export type ScopedTicketDetailControllerDeps = {
  api: ScopedTicketDetailApi;
  route: ScopedTicketDetailRoute;
  store?: ReadModelEntityStore;
  agentModelCatalogStore?: AgentModelCatalogStore;
  openFlowRunEventSource: (
    runId: string,
    owner: { repo?: string; worktree?: string },
    options: {
      onEvent: (event: FlowRunStreamEvent) => void;
      onError?: (error: Event) => void;
    }
  ) => StreamSubscription;
  invalidateTags?: (tags: ReadModelDependency[]) => Promise<void>;
  navigate: (path: string, options?: { replaceState?: boolean }) => Promise<void> | void;
};

export class ScopedTicketDetailController {
  private readonly api: ScopedTicketDetailApi;
  private readonly store: ReadModelEntityStore;
  private readonly agentModelCatalogStore: AgentModelCatalogStore;
  private readonly openFlowRunEventSource: ScopedTicketDetailControllerDeps['openFlowRunEventSource'];
  private readonly invalidateTags: (tags: ReadModelDependency[]) => Promise<void>;
  private readonly navigate: ScopedTicketDetailControllerDeps['navigate'];
  private readonly listeners = new Set<(state: ScopedTicketDetailControllerState) => void>();
  private unsubscribeReadModels: (() => void) | null = null;
  private unsubscribeAgentModelCatalog: (() => void) | null = null;
  private streamSubscription: StreamSubscription | null = null;
  private readModelState: ReadModelEntityState;
  private detailRequestSeq = 0;
  private currentPath: string | null;
  state: ScopedTicketDetailControllerState;

  constructor(deps: ScopedTicketDetailControllerDeps) {
    this.api = deps.api;
    this.store = deps.store ?? readModelEntityStore;
    this.agentModelCatalogStore = deps.agentModelCatalogStore ?? createAgentModelCatalogStore(deps.api.pma);
    this.openFlowRunEventSource = deps.openFlowRunEventSource;
    this.invalidateTags = deps.invalidateTags ?? invalidateReadModelTags;
    this.navigate = deps.navigate;
    this.currentPath = deps.route.currentPath ?? null;
    this.readModelState = this.store.snapshot();
    this.state = initialState(deps.route);
  }

  subscribe(listener: (state: ScopedTicketDetailControllerState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  mount(): void {
    this.unsubscribeReadModels = this.store.subscribe((state) => {
      this.readModelState = state;
    });
    this.unsubscribeAgentModelCatalog = this.agentModelCatalogStore.subscribe((state) => {
      this.applyAgentModelCatalogState(state);
    });
    void this.agentModelCatalogStore.ensureLoaded();
  }

  destroy(): void {
    this.unsubscribeReadModels?.();
    this.unsubscribeReadModels = null;
    this.unsubscribeAgentModelCatalog?.();
    this.unsubscribeAgentModelCatalog = null;
    this.closeFlowStream();
  }

  setRoute(route: ScopedTicketDetailRoute): void {
    const sameRoute =
      route.ownerScope.kind === this.state.ownerScope.kind &&
      route.ownerScope.id === this.state.ownerScope.id &&
      (route.ownerScope.parentRepoId ?? null) === (this.state.ownerScope.parentRepoId ?? null) &&
      route.ticketId === this.state.ticketId &&
      (route.currentPath ?? null) === this.currentPath;
    if (sameRoute) return;
    this.currentPath = route.currentPath ?? null;
    this.detailRequestSeq += 1;
    this.closeFlowStream();
    this.setState({
      ownerScope: route.ownerScope,
      ticketId: route.ticketId,
      detail: null,
      loading: true,
      error: null,
      sectionIssues: [],
      actionStatus: null,
      saveStatus: null,
      currentRunId: null,
      dispatchHistory: [],
      flowEvents: [],
      workerActivity: buildTicketWorkerActivity([], [])
    });
    void this.loadTicketDetail(true, route.ownerScope, route.ticketId);
  }

  async loadTicketDetail(
    showLoading = true,
    ownerScope = this.state.ownerScope,
    routeTicketId = this.state.ticketId
  ): Promise<void> {
    const requestSeq = ++this.detailRequestSeq;
    const isCurrentRequest = () =>
      requestSeq === this.detailRequestSeq &&
      ownerScope.kind === this.state.ownerScope.kind &&
      ownerScope.id === this.state.ownerScope.id &&
      routeTicketId === this.state.ticketId;
    if (showLoading) this.setState({ loading: true });
    this.setState({ error: null, sectionIssues: [] });
    if (showLoading) {
      const cached = renderScopedTicketCachedDetail(ownerScope, routeTicketId, {
        store: this.store,
        readModelState: this.readModelState
      });
      if (cached) {
        this.setState({ detail: cached.detail, loading: false });
      }
    }

    const session = await loadScopedTicketDetailSession(this.api, ownerScope, routeTicketId, {
      currentPath: this.currentPath ?? undefined,
      store: this.store
    });
    if (!isCurrentRequest()) return;
    if (!session.ok) {
      this.setState({ error: session.error, loading: false });
      return;
    }
    if (session.redirectTo) {
      this.setState({ loading: false });
      await this.navigate(session.redirectTo, { replaceState: true });
      return;
    }
    this.setState({
      currentRunId: session.currentRunId,
      detail: session.detail,
      sectionIssues: session.sectionIssues,
      dispatchHistory: session.dispatches,
      loading: false,
      workerActivity: buildTicketWorkerActivity(session.dispatches, this.state.flowEvents)
    });
    if (session.currentRunId) this.connectFlowStream(session.currentRunId, ownerScope);
  }

  async runCommand(command: 'resume' | 'bootstrap'): Promise<void> {
    const owner = this.state.ownerScope;
    const scopeLabel = owner.kind === 'repo' ? 'repo' : 'worktree';
    this.setState({
      actionStatus: command === 'resume' ? `Continuing ${scopeLabel} ticket flow...` : `Retrying ${scopeLabel} ticket flow...`
    });
    const ownerId = owner.id;
    const path =
      command === 'resume' && this.state.currentRunId
        ? `/repos/${encodeURIComponent(ownerId)}/api/flows/${encodeURIComponent(this.state.currentRunId)}/resume`
        : `/repos/${encodeURIComponent(ownerId)}/api/flows/ticket_flow/bootstrap`;
    const result = await this.api.requestJson(path, {
      method: 'POST',
      body: command === 'bootstrap' ? { once: false } : undefined
    });
    this.setState({ actionStatus: result.ok ? 'Ticket flow command accepted.' : result.error.message });
    await this.loadTicketDetail(false);
  }

  async saveTicket(payload: TicketEditPayload): Promise<boolean> {
    const detail = this.state.detail;
    if (!detail) return false;
    const ticketNumber = Number(detail.routeId);
    if (!Number.isInteger(ticketNumber)) {
      this.setState({ saveStatus: 'This ticket cannot be edited until it has a numeric TICKET index.' });
      return false;
    }

    this.setState({ saveStatus: 'Saving ticket...' });
    const owner = this.state.ownerScope;
    const result = await this.api.ticketFlow.updateTicket(
      ticketNumber,
      buildTicketUpdateContent(detail, payload),
      owner.kind === 'repo' ? { repo: owner.id } : { worktree: owner.id }
    );
    this.setState({ saveStatus: result.ok ? 'Ticket saved.' : result.error.message });
    if (result.ok) {
      await this.invalidateTags(this.ownerMutationTags(owner, this.state.ticketId, false));
      await this.loadTicketDetail(false);
    }
    return result.ok;
  }

  async repairWithPma(ticket: TicketDetailViewModel): Promise<void> {
    this.setState({ actionStatus: 'Creating PMA repair chat...' });
    const createResult = await this.api.pma.createChat(buildTicketRepairChatCreatePayload(ticket));
    if (!createResult.ok) {
      this.setState({ actionStatus: createResult.error.message });
      return;
    }
    const sendResult = await this.api.pma.sendMessage(
      createResult.data.id,
      buildManagedThreadMessagePayload(buildTicketRepairPrompt(ticket), '', false)
    );
    if (!sendResult.ok) {
      this.setState({ actionStatus: sendResult.error.message });
      return;
    }
    await this.invalidateTags([
      readModelEntityTags.chatIndex,
      readModelEntityTags.chat(createResult.data.id),
      ...this.ownerMutationTags(this.state.ownerScope, this.state.ticketId, true)
    ]);
    await this.navigate(chatRoute(createResult.data.id));
  }

  private connectFlowStream(runId: string, ownerScope: ScopedTicketDetailOwnerScope): void {
    this.closeFlowStream();
    this.streamSubscription = this.openFlowRunEventSource(
      runId,
      ownerScope.kind === 'repo' ? { repo: ownerScope.id } : { worktree: ownerScope.id },
      {
        onEvent: (event) => {
          const payload = { ...event.payload, seq: event.payload.seq ?? event.id };
          const flowEvents = [...this.state.flowEvents, payload].slice(-120);
          this.setState({
            flowEvents,
            workerActivity: buildTicketWorkerActivity(this.state.dispatchHistory, flowEvents)
          });
          if (ticketFlowEventShouldReload(payload)) {
            void this.loadTicketDetail(false, ownerScope, this.state.ticketId);
            this.closeFlowStream();
          }
        },
        onError: () => {
          void this.loadTicketDetail(false, ownerScope, this.state.ticketId);
        }
      }
    );
  }

  private closeFlowStream(): void {
    this.streamSubscription?.close();
    this.streamSubscription = null;
  }

  private applyAgentModelCatalogState(catalog: AgentModelCatalogState): void {
    this.setState({ agents: catalog.agents, modelCatalogs: catalog.modelCatalogs });
  }

  private ownerMutationTags(
    owner: ScopedTicketDetailOwnerScope,
    ticketId: string,
    omitTicketIndex: boolean
  ): ReadModelDependency[] {
    const ownerTag = owner.kind === 'repo' ? readModelEntityTags.repo(owner.id) : readModelEntityTags.worktree(owner.id);
    return omitTicketIndex
      ? [readModelEntityTags.ticket(ticketId), ownerTag]
      : [readModelEntityTags.ticket(ticketId), readModelEntityTags.ticketIndex, ownerTag];
  }

  private setState(patch: Partial<ScopedTicketDetailControllerState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener(this.state));
  }
}

export function createScopedTicketDetailController(
  deps: ScopedTicketDetailControllerDeps
): ScopedTicketDetailController {
  return new ScopedTicketDetailController(deps);
}

function initialState(route: ScopedTicketDetailRoute): ScopedTicketDetailControllerState {
  return {
    ownerScope: route.ownerScope,
    ticketId: route.ticketId,
    detail: null,
    loading: true,
    error: null,
    sectionIssues: [],
    actionStatus: null,
    saveStatus: null,
    currentRunId: null,
    dispatchHistory: [],
    flowEvents: [],
    workerActivity: buildTicketWorkerActivity([], []),
    agents: [],
    modelCatalogs: {}
  };
}
