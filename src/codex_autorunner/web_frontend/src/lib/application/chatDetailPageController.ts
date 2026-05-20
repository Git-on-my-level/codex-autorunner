import type { Readable } from 'svelte/store';
import type { ApiError, ApiResult, JsonRecord } from '$lib/api/client';
import {
  type ChatIndexWindowRequest,
  canonicalChatIndexWindowKey,
  selectPmaChats,
  selectRepoSummaries,
  selectWorktreeSummaries,
  type ReadModelEntityState,
  type ReadModelEntityStore,
  type ReadModelLoaderResult
} from '$lib/data';
import {
  activateChatDetailFromUrl,
  activateRequestedChatFromRows,
  archivedFilterForSelectedChat,
  clearCommittedDraftPlaceholderIfPersisted,
  initialChatDetailSessionState,
  loadPinnedChats,
  replacementForArchivedActiveChat,
  requestedChatDetailFromUrl,
  selectChatDetail,
  type ChatDetailSelectionCommand,
  type ChatDetailSessionState,
  type PinnedChatMap
} from './chatDetailSession';
import type { PmaChatSummary, SurfaceArtifact } from '$lib/viewModels/domain';
import {
  buildPmaChatScopeOptions,
  type PmaChatScopeOption
} from '$lib/viewModels/pmaChat';

type ChatIndexSessionState = {
  status: 'idle' | 'loading' | 'connected' | 'interrupted' | 'closed';
  error: ApiError | null;
};

export type ChatDetailPageIndexSession = {
  state: Readable<ChatIndexSessionState>;
  activate: (activation: {
    primaryRequest?: ChatIndexWindowRequest;
    companionRequests?: ChatIndexWindowRequest[];
    refresh?: boolean;
  }) => Promise<void>;
  start: () => void;
  stop: () => void;
  refresh: (request?: ChatIndexWindowRequest) => Promise<void>;
  setCompanionRequests: (requests: ChatIndexWindowRequest[]) => void;
};

export type ChatDetailPageLiveProjection = {
  activate: (chatId: string | null, options?: { quiet?: boolean }) => Promise<void>;
  refresh: (chatId: string, options?: { quiet?: boolean }) => Promise<void>;
  retry: (chatId: string) => void;
  close: () => void;
};

export type ChatDetailPageRoute = {
  chatId: string | null | undefined;
  searchParams: URLSearchParams;
  data?: {
    chatId?: string | null;
    activeDetail?: ReadModelLoaderResult | null;
    chatIndex?: ReadModelLoaderResult | null;
  };
};

export type ChatDetailPageSupportApi = {
  listFiles: () => Promise<ApiResult<SurfaceArtifact[]>>;
  listAgents: () => Promise<ApiResult<{ agents: JsonRecord[]; defaults: JsonRecord; default: string }>>;
  repoWorktreeTopology: (filter?: 'repo' | 'worktree' | 'all', limit?: number) => Promise<ApiResult<unknown>>;
  repoWorktreeRuntime: (filter?: 'repo' | 'worktree' | 'all', limit?: number) => Promise<ApiResult<unknown>>;
};

export type ChatDetailPageSupportData = {
  agents: JsonRecord[];
  defaults: JsonRecord;
  defaultAgent: string;
  scopeOptions: PmaChatScopeOption[];
};

type ControllerTimers = {
  setInterval: (handler: () => void, timeout: number) => unknown;
  clearInterval: (timer: unknown) => void;
  setTimeout: (handler: () => void, timeout: number) => unknown;
  clearTimeout: (timer: unknown) => void;
  now: () => number;
};

export type ChatDetailPageControllerDeps = {
  readModelStore: ReadModelEntityStore;
  chatIndexSession: ChatDetailPageIndexSession;
  liveProjection: ChatDetailPageLiveProjection;
  supportApi: ChatDetailPageSupportApi;
  readSessionState: () => ChatDetailSessionState;
  writeSessionState: (state: ChatDetailSessionState) => void;
  onReadModelState: (state: ReadModelEntityState) => void;
  onLoadingChats: (loading: boolean) => void;
  onChatError: (error: ApiError | null) => void;
  onFilterArchived: () => void;
  onClockTick: (nowMs: number) => void;
  onPinnedChatsLoaded: (pinned: PinnedChatMap) => void;
  onInitialDraft: (draft: string) => void;
  onCreateInitialDraft: () => void;
  onSupportDataLoaded: (data: ChatDetailPageSupportData) => void;
  onSyncSelectors: () => void;
  onMarkRead: () => void;
  timers?: Partial<ControllerTimers>;
};

export class ChatDetailPageController {
  private readonly deps: ChatDetailPageControllerDeps;
  private readonly timers: ControllerTimers;
  private route: ChatDetailPageRoute | null = null;
  private currentRequest: ChatIndexWindowRequest = { filter: 'all', limit: 50 };
  private ticketRunGroupRequest: ChatIndexWindowRequest = { filter: 'ticket_runs', groupBy: 'ticket_run', limit: 50 };
  private readModelState: ReadModelEntityState;
  private unsubscribeReadModels: (() => void) | null = null;
  private unsubscribeChatIndexSession: (() => void) | null = null;
  private filterRefreshTimer: unknown = null;
  private activeClockInterval: unknown = null;

  constructor(deps: ChatDetailPageControllerDeps) {
    this.deps = deps;
    this.readModelState = deps.readModelStore.snapshot();
    this.timers = {
      setInterval: deps.timers?.setInterval ?? ((handler, timeout) => globalThis.setInterval(handler, timeout)),
      clearInterval: deps.timers?.clearInterval ?? ((timer) => globalThis.clearInterval(timer as ReturnType<typeof setInterval>)),
      setTimeout: deps.timers?.setTimeout ?? ((handler, timeout) => globalThis.setTimeout(handler, timeout)),
      clearTimeout: deps.timers?.clearTimeout ?? ((timer) => globalThis.clearTimeout(timer as ReturnType<typeof setTimeout>)),
      now: deps.timers?.now ?? (() => Date.now())
    };
  }

  mount(input: {
    route: ChatDetailPageRoute;
    currentRequest: ChatIndexWindowRequest;
    ticketRunGroupRequest: ChatIndexWindowRequest;
  }): void {
    this.route = input.route;
    this.currentRequest = input.currentRequest;
    this.ticketRunGroupRequest = input.ticketRunGroupRequest;
    this.unsubscribeReadModels = this.deps.readModelStore.subscribe((state) => this.handleReadModelState(state));
    this.unsubscribeChatIndexSession = this.deps.chatIndexSession.state.subscribe((session) => this.handleIndexSessionState(session));
    void this.deps.chatIndexSession.activate({
      primaryRequest: this.currentRequest,
      companionRequests: [this.ticketRunGroupRequest],
      refresh: false
    });
    this.deps.chatIndexSession.start();
    this.deps.onPinnedChatsLoaded(loadPinnedChats());
    const initialDraft = input.route.searchParams.get('draft');
    if (initialDraft) this.deps.onInitialDraft(initialDraft);
    this.deps.onLoadingChats(!hasChatIndexProjection(this.deps.readModelStore.snapshot()));
    if (hasChatIndexProjection(this.deps.readModelStore.snapshot())) this.activateRequestedChatFromCurrentRows();
    if (initialDraft && !this.requestedDetailFromUrl()) this.deps.onCreateInitialDraft();
    void this.loadInitialSupportingData();
  }

  setRoute(route: ChatDetailPageRoute): void {
    this.route = route;
    const command = activateChatDetailFromUrl(this.readSession(), {
      detailId: this.requestedDetailFromUrl(),
      chats: this.requestedDetailFromUrl() ? this.currentChats() : [],
      hasCachedDetail: (chatId) => this.hasCachedDetail(chatId),
      activeDetailLoadResult: (chatId) => this.activeDetailLoadResult(chatId)
    });
    this.applySelectionCommand(command);
  }

  setIndexRequest(request: ChatIndexWindowRequest): void {
    this.currentRequest = request;
    const snapshot = this.deps.readModelStore.snapshot();
    this.deps.onLoadingChats(!snapshot.chatWindows[canonicalChatIndexWindowKey(request)] && !hasChatIndexProjection(snapshot));
    if (this.filterRefreshTimer) this.timers.clearTimeout(this.filterRefreshTimer);
    this.filterRefreshTimer = this.timers.setTimeout(() => {
      this.filterRefreshTimer = null;
      void this.deps.chatIndexSession.activate({ primaryRequest: request });
    }, 180);
  }

  setProgressStatus(status: string | null | undefined): void {
    if (status === 'running') this.startActiveClock();
    else this.stopActiveClock();
  }

  clearCommittedDraftPlaceholder(persistedChats: PmaChatSummary[]): void {
    this.writeSession(clearCommittedDraftPlaceholderIfPersisted(this.readSession(), persistedChats));
  }

  async selectChat(chatId: string, options: { syncUrl?: boolean } = {}): Promise<ChatDetailSelectionCommand> {
    const cached = this.hasCachedDetail(chatId);
    const loaderResult = this.activeDetailLoadResult(chatId);
    const command = selectChatDetail(this.readSession(), chatId, {
      cached,
      loaderOwnsInitialDetail: Boolean(loaderResult && loaderResult.status !== 'cold'),
      activeError: loaderResult?.status === 'error' ? loaderResult.error : null,
      syncUrl: options.syncUrl ?? false
    });
    this.applySelectionCommand(command);
    return command;
  }

  async refreshActive(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    await this.deps.liveProjection.refresh(chatId, options);
  }

  retryStream(chatId: string | null): void {
    if (!chatId || chatId.startsWith('draft:pma:')) return;
    this.deps.liveProjection.retry(chatId);
  }

  closeStream(): void {
    this.deps.liveProjection.close();
  }

  async refreshIndex(request?: ChatIndexWindowRequest): Promise<void> {
    await this.deps.chatIndexSession.refresh(request);
  }

  destroy(): void {
    this.unsubscribeReadModels?.();
    this.unsubscribeChatIndexSession?.();
    this.deps.chatIndexSession.stop();
    void this.deps.chatIndexSession.activate({ companionRequests: [], refresh: false });
    if (this.filterRefreshTimer) this.timers.clearTimeout(this.filterRefreshTimer);
    this.stopActiveClock();
    this.deps.liveProjection.close();
  }

  private handleReadModelState(state: ReadModelEntityState): void {
    const previous = this.readModelState;
    const replacementChatId = replacementForArchivedActiveChat(
      selectPmaChats(previous, this.currentRequest),
      selectPmaChats(state, this.currentRequest),
      this.readSession().activeChatId
    );
    this.readModelState = state;
    this.deps.onReadModelState(state);
    if (state.chatWindows[canonicalChatIndexWindowKey(this.currentRequest)] || hasChatIndexProjection(state)) {
      this.deps.onLoadingChats(false);
      this.deps.onChatError(null);
      this.activateRequestedChatFromCurrentRows();
    }
    if (replacementChatId) void this.selectChat(replacementChatId);
  }

  private handleIndexSessionState(session: ChatIndexSessionState): void {
    if (session.status === 'loading' && !this.deps.readModelStore.snapshot().chatWindows[canonicalChatIndexWindowKey(this.currentRequest)]) {
      this.deps.onLoadingChats(true);
    }
    if (session.error) {
      this.deps.onChatError(session.error);
      this.deps.onLoadingChats(false);
    }
  }

  private activateRequestedChatFromCurrentRows(): void {
    const loadedChats = this.currentChats();
    const command = activateRequestedChatFromRows(this.readSession(), {
      loadedChats,
      requestedChatId: this.requestedDetailFromUrl(),
      hasCachedDetail: (chatId) => this.hasCachedDetail(chatId)
    });
    this.applySelectionCommand(command);
    if (archivedFilterForSelectedChat(loadedChats, command.state.activeChatId)) this.deps.onFilterArchived();
  }

  private applySelectionCommand(command: ChatDetailSelectionCommand): void {
    if (command.runtime) void this.deps.liveProjection.activate(command.runtime.chatId, { quiet: command.runtime.quiet });
    this.writeSession(command.state);
    if (command.syncSelectors) this.deps.onSyncSelectors();
    if (command.markRead) this.deps.onMarkRead();
  }

  private async loadInitialSupportingData(): Promise<void> {
    const [artifactResult, agentResult, topologyResult, runtimeResult] = await Promise.all([
      this.deps.supportApi.listFiles(),
      this.deps.supportApi.listAgents(),
      this.deps.supportApi.repoWorktreeTopology('all', 50),
      this.deps.supportApi.repoWorktreeRuntime('all', 50)
    ]);
    if (artifactResult.ok) this.deps.readModelStore.setPmaArtifacts('__global__', artifactResult.data);
    if (topologyResult.ok) this.deps.readModelStore.applyRepoWorktreeTopologySnapshot(topologyResult.data as Parameters<ReadModelEntityStore['applyRepoWorktreeTopologySnapshot']>[0]);
    if (runtimeResult.ok) this.deps.readModelStore.applyRepoWorktreeRuntimeSnapshot(runtimeResult.data as Parameters<ReadModelEntityStore['applyRepoWorktreeRuntimeSnapshot']>[0]);
    if (!agentResult.ok) return;
    const scopeState = this.deps.readModelStore.snapshot();
    this.deps.onSupportDataLoaded({
      agents: agentResult.data.agents,
      defaults: agentResult.data.defaults,
      defaultAgent: agentResult.data.default,
      scopeOptions: buildPmaChatScopeOptions(
        topologyResult.ok ? selectRepoSummaries(scopeState) : [],
        topologyResult.ok ? selectWorktreeSummaries(scopeState) : []
      )
    });
  }

  private requestedDetailFromUrl(): string | null {
    const route = this.route;
    if (!route) return null;
    return requestedChatDetailFromUrl(route.chatId, route.searchParams);
  }

  private activeDetailLoadResult(chatId: string): ReadModelLoaderResult | null {
    const data = this.route?.data;
    if (data?.chatId !== chatId) return null;
    return data.activeDetail ?? null;
  }

  private hasCachedDetail(chatId: string): boolean {
    const state = this.deps.readModelStore.snapshot();
    return Boolean(
      state.chatTranscripts[chatId]?.order.length ||
      state.pmaProgress[chatId] ||
      state.pmaQueues[chatId]?.length ||
      state.timelines[chatId]?.order.length ||
      state.chatDetails[chatId]?.thread
    );
  }

  private currentChats(): PmaChatSummary[] {
    return selectPmaChats(this.deps.readModelStore.snapshot(), this.currentRequest);
  }

  private readSession(): ChatDetailSessionState {
    return {
      ...initialChatDetailSessionState(),
      ...this.deps.readSessionState()
    };
  }

  private writeSession(state: ChatDetailSessionState): void {
    this.deps.writeSessionState(state);
  }

  private startActiveClock(): void {
    if (this.activeClockInterval !== null) return;
    this.deps.onClockTick(this.timers.now());
    this.activeClockInterval = this.timers.setInterval(() => this.deps.onClockTick(this.timers.now()), 1000);
  }

  private stopActiveClock(): void {
    if (this.activeClockInterval === null) return;
    this.timers.clearInterval(this.activeClockInterval);
    this.activeClockInterval = null;
  }
}

export function createChatDetailPageController(deps: ChatDetailPageControllerDeps): ChatDetailPageController {
  return new ChatDetailPageController(deps);
}

function hasChatIndexProjection(state: ReadModelEntityState): boolean {
  return Boolean(state.chatIndexCursor || state.chatOrder.length > 0);
}
