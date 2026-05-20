import type { ApiError, ApiResult, JsonRecord, PmaThreadQueue } from '$lib/api/client';
import {
  openChatTranscriptEventSource,
  shouldUseChatTranscriptStream,
  type ChatTranscriptStreamEvent,
  type StreamSubscription,
  type TranscriptStreamOptions
} from '$lib/api/streaming';
import type { ReadModelEntityStore } from '$lib/data/readModelStore';
import {
  mergePmaProgressUpdate,
  mergeTranscriptSnapshotWithPendingOptimistic
} from '$lib/application/pmaChatArchitecture';
import { mapPmaRunProgress, type PmaChatSummary, type PmaRunProgress } from '$lib/viewModels/domain';
import {
  mapChatTranscriptRows,
  type ChatTranscriptCard,
  type ChatTranscriptSnapshot
} from '$lib/viewModels/pmaChat';

export type ChatDetailStreamState = 'idle' | 'connecting' | 'connected' | 'interrupted';

export type ChatDetailLiveProjectionState = {
  loadingActive: boolean;
  activeError: ApiError | null;
  streamState: ChatDetailStreamState;
  streamError: string | null;
};

export type ChatDetailLiveProjectionApi = {
  getTranscript: (chatId: string, request?: { limit?: number }) => Promise<ApiResult<ChatTranscriptSnapshot>>;
  getQueue: (chatId: string) => Promise<ApiResult<PmaThreadQueue>>;
};

export type ChatDetailLiveProjectionOptions = {
  transcriptLimit?: number;
  refreshDelayMs?: number;
  queueRefreshDelayMs?: number;
  repairDelayMs?: number;
};

export type ChatDetailLiveProjectionDeps = {
  api: ChatDetailLiveProjectionApi;
  readModelStore: ReadModelEntityStore;
  getChatSummary: (chatId: string) => PmaChatSummary | null;
  onStateChange?: (state: ChatDetailLiveProjectionState) => void;
  openStream?: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
  shouldUseStream?: typeof shouldUseChatTranscriptStream;
  setTimeout?: (handler: () => void, timeout: number) => unknown;
  clearTimeout?: (timer: unknown) => void;
  now?: () => number;
  options?: ChatDetailLiveProjectionOptions;
};

const DEFAULT_TRANSCRIPT_LIMIT = 200;
const DEFAULT_REFRESH_DELAY_MS = 600;
const DEFAULT_QUEUE_REFRESH_DELAY_MS = 600;
const DEFAULT_REPAIR_DELAY_MS = 900;

export class ChatDetailLiveProjection {
  private readonly api: ChatDetailLiveProjectionApi;
  private readonly readModelStore: ReadModelEntityStore;
  private readonly getChatSummary: (chatId: string) => PmaChatSummary | null;
  private readonly onStateChange: ((state: ChatDetailLiveProjectionState) => void) | undefined;
  private readonly openStream: (chatId: string, options: TranscriptStreamOptions) => StreamSubscription;
  private readonly shouldUseStream: typeof shouldUseChatTranscriptStream;
  private readonly setTimer: (handler: () => void, timeout: number) => unknown;
  private readonly clearTimer: (timer: unknown) => void;
  private readonly now: () => number;
  private readonly transcriptLimit: number;
  private readonly refreshDelayMs: number;
  private readonly queueRefreshDelayMs: number;
  private readonly repairDelayMs: number;

  private state: ChatDetailLiveProjectionState = {
    loadingActive: false,
    activeError: null,
    streamState: 'idle',
    streamError: null
  };
  private activeChatId: string | null = null;
  private subscription: StreamSubscription | null = null;
  private activeRefreshSeq = 0;
  private activeQueueRefreshSeq = 0;
  private pendingRefreshTimer: unknown = null;
  private pendingRefreshReason: 'terminal' | 'repair' | null = null;
  private pendingQueueRefreshTimer: unknown = null;
  private refreshedTerminalTurnId: string | null = null;

  constructor(deps: ChatDetailLiveProjectionDeps) {
    this.api = deps.api;
    this.readModelStore = deps.readModelStore;
    this.getChatSummary = deps.getChatSummary;
    this.onStateChange = deps.onStateChange;
    this.openStream = deps.openStream ?? openChatTranscriptEventSource;
    this.shouldUseStream = deps.shouldUseStream ?? shouldUseChatTranscriptStream;
    this.setTimer = deps.setTimeout ?? ((handler, timeout) => globalThis.setTimeout(handler, timeout));
    this.clearTimer = deps.clearTimeout ?? ((timer) => globalThis.clearTimeout(timer as ReturnType<typeof setTimeout>));
    this.now = deps.now ?? (() => Date.now());
    this.transcriptLimit = deps.options?.transcriptLimit ?? DEFAULT_TRANSCRIPT_LIMIT;
    this.refreshDelayMs = deps.options?.refreshDelayMs ?? DEFAULT_REFRESH_DELAY_MS;
    this.queueRefreshDelayMs = deps.options?.queueRefreshDelayMs ?? DEFAULT_QUEUE_REFRESH_DELAY_MS;
    this.repairDelayMs = deps.options?.repairDelayMs ?? DEFAULT_REPAIR_DELAY_MS;
  }

  snapshot(): ChatDetailLiveProjectionState {
    return { ...this.state };
  }

  replaceState(next: Partial<ChatDetailLiveProjectionState>): void {
    this.setState(next);
  }

  async activate(
    chatId: string | null,
    options: { quiet?: boolean; sessionActiveError?: ApiError | null } = {}
  ): Promise<void> {
    if (!chatId) {
      if ('sessionActiveError' in options) {
        this.state.activeError = options.sessionActiveError ?? null;
      }
      this.close();
      this.setState({
        loadingActive: false,
        ...('sessionActiveError' in options ? {} : { activeError: null })
      });
      return;
    }
    if (this.activeChatId !== chatId) {
      this.activeRefreshSeq += 1;
      this.activeQueueRefreshSeq += 1;
      this.clearScheduledRefreshes();
      this.closeStream();
      this.activeChatId = chatId;
      this.refreshedTerminalTurnId = null;
    }
    await this.refreshActive(chatId, options);
  }

  async refresh(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    if (this.activeChatId !== chatId) {
      await this.activate(chatId, options);
      return;
    }
    await this.refreshActive(chatId, options);
  }

  private async refreshActive(chatId: string, options: { quiet?: boolean } = {}): Promise<void> {
    const refreshSeq = ++this.activeRefreshSeq;
    if (!options.quiet) this.setState({ loadingActive: true, activeError: null });

    let missingThreadError: ApiError | null = null;
    const transcriptTask = this.api.getTranscript(chatId, { limit: this.transcriptLimit }).then((result) => {
      if (!this.isCurrent(chatId, refreshSeq)) return;
      if (result.ok) {
        this.replaceTranscriptPreservingPendingOptimistic(chatId, result.data.rows);
        if (result.data.status) this.updateProgress(result.data.status);
      } else if (isMissingManagedThreadError(result.error)) {
        missingThreadError = result.error;
        this.readModelStore.replaceChatTranscript(chatId, []);
      } else if (!options.quiet) {
        this.setState({ activeError: result.error });
      }
    });
    const queueTask = this.api.getQueue(chatId).then((result) => {
      if (!this.isCurrent(chatId, refreshSeq)) return;
      if (result.ok) {
        this.readModelStore.setPmaQueue(chatId, result.data.queuedTurns);
      } else if (isMissingManagedThreadError(result.error)) {
        missingThreadError = result.error;
        this.readModelStore.setPmaQueue(chatId, []);
      }
    });

    await Promise.all([transcriptTask, queueTask]);
    if (!this.isCurrent(chatId, refreshSeq)) return;
    if (missingThreadError) {
      this.setState({ activeError: missingThreadError, loadingActive: false });
      this.closeStream();
      return;
    }
    this.ensureStreamAfterSnapshot(chatId);
    if (!options.quiet || this.state.loadingActive) this.setState({ loadingActive: false });
  }

  async refreshQueue(chatId: string): Promise<void> {
    const refreshSeq = ++this.activeQueueRefreshSeq;
    const result = await this.api.getQueue(chatId);
    if (this.activeChatId !== chatId || refreshSeq !== this.activeQueueRefreshSeq) return;
    if (result.ok) {
      this.readModelStore.setPmaQueue(chatId, result.data.queuedTurns);
    } else if (isMissingManagedThreadError(result.error)) {
      this.readModelStore.setPmaQueue(chatId, []);
    }
  }

  connect(chatId: string): void {
    if (this.activeChatId !== chatId) {
      this.activeRefreshSeq += 1;
      this.activeQueueRefreshSeq += 1;
      this.clearScheduledRefreshes();
      this.closeStream();
      this.activeChatId = chatId;
    } else {
      this.closeStream();
    }
    const seedProgress = this.currentProgress(chatId);
    const seedChat = this.getChatSummary(chatId);
    if (!this.shouldUseStream(seedChat, seedProgress, this.currentQueueDepth(chatId))) {
      this.setState({ streamState: 'idle', streamError: null });
      return;
    }
    this.setState({ streamState: 'connecting', streamError: null });
    this.refreshedTerminalTurnId = null;
    this.subscription = this.openStream(chatId, {
      sinceEventId: seedProgress?.lastEventId,
      sinceManagedTurnId: seedProgress?.id,
      onStatus: (status) => this.handleStreamStatus(chatId, status),
      onEvent: (event) => this.handleStreamEvent(chatId, event),
      onError: () => this.handleStreamError(chatId)
    });
  }

  retry(chatId: string): void {
    this.connect(chatId);
    void this.refresh(chatId, { quiet: true });
  }

  close(): void {
    this.activeChatId = null;
    this.activeRefreshSeq += 1;
    this.activeQueueRefreshSeq += 1;
    this.clearScheduledRefreshes();
    this.closeStream();
  }

  private handleStreamStatus(chatId: string, status: 'connecting' | 'connected' | 'interrupted' | 'closed'): void {
    if (this.activeChatId !== chatId) return;
    if (status === 'connecting' && this.state.streamState !== 'connected') this.setState({ streamState: 'connecting' });
    if (status === 'connected') {
      this.clearPendingRepairRefresh();
      this.setState({ streamState: 'connected', streamError: null });
    }
    if (status === 'interrupted') this.setState({ streamState: 'interrupted' });
  }

  private handleStreamEvent(chatId: string, event: ChatTranscriptStreamEvent): void {
    if (this.activeChatId !== chatId) return;
    this.clearPendingRepairRefresh();
    this.setState({ streamState: 'connected' });
    if (event.kind === 'transcript_snapshot') {
      const rows = mapChatTranscriptRows(event.payload.rows);
      this.replaceTranscriptPreservingPendingOptimistic(chatId, rows);
      const status = event.payload.status;
      if (status && typeof status === 'object' && !Array.isArray(status)) {
        const nextProgress = mapPmaRunProgress(status as JsonRecord);
        this.updateProgress(nextProgress);
        if (nextProgress.terminal && nextProgress.id && transcriptHasAssistantMessageForTurn(rows, nextProgress.id)) {
          this.refreshedTerminalTurnId = nextProgress.id;
          this.scheduleQueueRefresh(chatId, this.queueRefreshDelayMs);
        }
      }
      return;
    }
    if (event.kind === 'transcript_append') {
      this.readModelStore.upsertChatTranscriptCards(chatId, mapChatTranscriptRows(event.payload.rows));
      return;
    }
    if (event.kind === 'transcript_patch') {
      const status = event.payload.status;
      if (!status || typeof status !== 'object' || Array.isArray(status)) return;
      const nextProgress = mapPmaRunProgress(status as JsonRecord);
      this.updateProgress(nextProgress);
      if (nextProgress.terminal && nextProgress.id && this.refreshedTerminalTurnId !== nextProgress.id) {
        this.refreshedTerminalTurnId = nextProgress.id;
        this.scheduleRefresh(chatId, this.refreshDelayMs, 'terminal');
      }
      if (nextProgress.streamShouldClose) this.closeStream();
    }
  }

  private handleStreamError(chatId: string): void {
    if (this.activeChatId !== chatId) return;
    if (this.currentProgress(chatId)?.streamShouldClose) {
      this.closeStream();
      return;
    }
    this.setState({
      streamState: 'interrupted',
      streamError: 'Live chat updates were interrupted. Reconnecting and repairing from the latest snapshot.'
    });
    this.scheduleRefresh(chatId, this.repairDelayMs, 'repair');
  }

  private ensureStreamAfterSnapshot(chatId: string): void {
    if (this.activeChatId !== chatId || this.subscription) return;
    const seedProgress = this.currentProgress(chatId);
    const seedChat = this.getChatSummary(chatId);
    if (this.shouldUseStream(seedChat, seedProgress, this.currentQueueDepth(chatId))) this.connect(chatId);
  }

  private scheduleRefresh(chatId: string, delayMs: number, reason: 'terminal' | 'repair'): void {
    if (this.pendingRefreshTimer) this.clearTimer(this.pendingRefreshTimer);
    this.pendingRefreshReason = reason;
    this.pendingRefreshTimer = this.setTimer(() => {
      this.pendingRefreshTimer = null;
      this.pendingRefreshReason = null;
      if (this.activeChatId === chatId) void this.refreshActive(chatId, { quiet: true });
    }, delayMs);
  }

  private scheduleQueueRefresh(chatId: string, delayMs: number): void {
    if (this.pendingQueueRefreshTimer) this.clearTimer(this.pendingQueueRefreshTimer);
    this.pendingQueueRefreshTimer = this.setTimer(() => {
      this.pendingQueueRefreshTimer = null;
      if (this.activeChatId === chatId) void this.refreshQueue(chatId);
    }, delayMs);
  }

  private clearScheduledRefreshes(): void {
    if (this.pendingRefreshTimer) this.clearTimer(this.pendingRefreshTimer);
    if (this.pendingQueueRefreshTimer) this.clearTimer(this.pendingQueueRefreshTimer);
    this.pendingRefreshTimer = null;
    this.pendingRefreshReason = null;
    this.pendingQueueRefreshTimer = null;
  }

  private clearPendingRepairRefresh(): void {
    if (!this.pendingRefreshTimer || this.pendingRefreshReason !== 'repair') return;
    this.clearTimer(this.pendingRefreshTimer);
    this.pendingRefreshTimer = null;
    this.pendingRefreshReason = null;
  }

  private closeStream(): void {
    this.subscription?.close();
    this.subscription = null;
    this.setState({ streamState: 'idle' });
  }

  private updateProgress(nextProgress: PmaRunProgress): void {
    const chatId = nextProgress.chatId ?? this.activeChatId;
    if (!chatId) return;
    this.readModelStore.setPmaProgress(
      chatId,
      mergePmaProgressUpdate(this.currentProgress(chatId), nextProgress, this.now())
    );
  }

  private currentProgress(chatId: string): PmaRunProgress | null {
    return this.readModelStore.snapshot().pmaProgress[chatId] ?? null;
  }

  private currentQueueDepth(chatId: string): number {
    return this.readModelStore.snapshot().pmaQueues[chatId]?.length ?? 0;
  }

  private replaceTranscriptPreservingPendingOptimistic(chatId: string, rows: ChatTranscriptCard[]): void {
    const existing = this.readModelStore.snapshot().chatTranscripts[chatId];
    this.readModelStore.replaceChatTranscript(
      chatId,
      mergeTranscriptSnapshotWithPendingOptimistic(existing, rows)
    );
  }

  private isCurrent(chatId: string, refreshSeq: number): boolean {
    return this.activeChatId === chatId && refreshSeq === this.activeRefreshSeq;
  }

  private setState(next: Partial<ChatDetailLiveProjectionState>): void {
    const merged = { ...this.state, ...next };
    if (
      merged.loadingActive === this.state.loadingActive &&
      merged.activeError === this.state.activeError &&
      merged.streamState === this.state.streamState &&
      merged.streamError === this.state.streamError
    ) {
      return;
    }
    this.state = merged;
    this.onStateChange?.(this.snapshot());
  }
}

export function createChatDetailLiveProjection(deps: ChatDetailLiveProjectionDeps): ChatDetailLiveProjection {
  return new ChatDetailLiveProjection(deps);
}

export function isMissingManagedThreadError(error: ApiError): boolean {
  return error.status === 404 && error.message.toLowerCase().includes('managed thread not found');
}

export function transcriptHasAssistantMessageForTurn(cards: ChatTranscriptCard[], turnId: string): boolean {
  return cards.some((card) =>
    card.kind === 'message' &&
    card.turnId === turnId &&
    card.message.role === 'assistant' &&
    card.message.text.trim().length > 0
  );
}
