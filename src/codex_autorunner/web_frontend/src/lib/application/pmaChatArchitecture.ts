import type { PmaQueuedTurn } from '$lib/api/client';
import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import {
  buildPmaStatusBar,
  compactChatTranscriptCards,
  type ChatTranscriptCard,
  type PmaStatusBar
} from '$lib/viewModels/pmaChat';
import type { ChatDetailStreamState } from './chatDetailLiveProjection';

type MessageTranscriptCard = Extract<ChatTranscriptCard, { kind: 'message' }>;

export type ChatTranscriptListItem =
  | { kind: 'card'; id: string; card: ChatTranscriptCard }
  | { kind: 'typing'; id: string; title: string }
  | { kind: 'shared-files'; id: string };

export type ChatDetailDisplayReadModelInput = {
  transcriptCards: ChatTranscriptCard[];
  queuedTurns: PmaQueuedTurn[];
  displayedProgress: PmaRunProgress | null;
  activeChat: PmaChatSummary | null;
  assistantSharedFileCount: number;
  streamState: ChatDetailStreamState;
  loadingActive: boolean;
  activeError: unknown;
  draft: string;
  pendingAttachmentCount: number;
};

export type ChatDetailDisplayReadModel = {
  activeCards: ChatTranscriptCard[];
  displayTranscriptCards: ChatTranscriptCard[];
  lastAssistantMessageCard: ChatTranscriptCard | null;
  statusBar: PmaStatusBar | null;
  streamingMessageId: string | null;
  showTypingIndicator: boolean;
  transcriptListItems: ChatTranscriptListItem[];
  srAnnouncement: string;
  showStreamHealthAside: boolean;
  showStatusBar: boolean;
  chatHasActivity: boolean;
  showStartPicker: boolean;
  hasRunnableDraft: boolean;
  canInterruptWithDraft: boolean;
  composerWillQueue: boolean;
  queueDepthForCommands: number;
};

export type PmaChatArchitecturePrinciple =
  | 'backendTranscriptProjection'
  | 'thinRouteSurface'
  | 'applicationCommandBoundary'
  | 'capabilityAdapters'
  | 'deterministicRepair'
  | 'windowedRendering'
  | 'contractTests';

export type PmaChatArchitectureSignal = {
  principle: PmaChatArchitecturePrinciple;
  satisfied: boolean;
  weight: number;
  detail: string;
};

export type PmaChatArchitectureGoalEvaluation = {
  score: number;
  targetScore: number;
  satisfied: boolean;
  strengths: string[];
  gaps: string[];
  signals: PmaChatArchitectureSignal[];
};

export const PMA_CHAT_ARCHITECTURE_TARGET_SCORE = 0.92;

export const PMA_CHAT_ARCHITECTURE_GOAL: Record<PmaChatArchitecturePrinciple, string> = {
  backendTranscriptProjection:
    'Render the backend transcript projection as the source of truth; the frontend may hold only pending optimistic user rows.',
  thinRouteSurface:
    'Keep Svelte routes focused on binding UI controls to application services, not owning transcript or stream reconciliation rules.',
  applicationCommandBoundary:
    'Plan and execute chat commands through typed application-layer functions so command behavior is easy to unit test.',
  capabilityAdapters:
    'Plug new chat capabilities in through explicit command, stream, and projection adapters instead of page-local branching.',
  deterministicRepair:
    'Prefer cursor streams with snapshot repair and deterministic reconciliation over recurring quiet refresh loops.',
  windowedRendering:
    'Keep chat indexes and transcripts bounded or virtualized so large workspaces do not degrade page behavior.',
  contractTests:
    'Cover projection, command, stream, and optimistic reconciliation behavior with deterministic tests.'
};

export type PmaChatArchitectureGoalInput = {
  transcriptProjectionIsBackendOwned: boolean;
  routeOwnsTranscriptReconciliation: boolean;
  commandsUseApplicationPlans: boolean;
  capabilitiesHaveTypedBoundaries: boolean;
  usesCursorStreamRepair: boolean;
  usesUnboundedRendering: boolean;
  hasContractTests: boolean;
};

export function evaluatePmaChatArchitectureGoal(
  input: PmaChatArchitectureGoalInput
): PmaChatArchitectureGoalEvaluation {
  const signals: PmaChatArchitectureSignal[] = [
    signal(
      'backendTranscriptProjection',
      input.transcriptProjectionIsBackendOwned,
      4,
      PMA_CHAT_ARCHITECTURE_GOAL.backendTranscriptProjection
    ),
    signal(
      'thinRouteSurface',
      !input.routeOwnsTranscriptReconciliation,
      3,
      PMA_CHAT_ARCHITECTURE_GOAL.thinRouteSurface
    ),
    signal(
      'applicationCommandBoundary',
      input.commandsUseApplicationPlans,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.applicationCommandBoundary
    ),
    signal(
      'capabilityAdapters',
      input.capabilitiesHaveTypedBoundaries,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.capabilityAdapters
    ),
    signal(
      'deterministicRepair',
      input.usesCursorStreamRepair,
      3,
      PMA_CHAT_ARCHITECTURE_GOAL.deterministicRepair
    ),
    signal(
      'windowedRendering',
      !input.usesUnboundedRendering,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.windowedRendering
    ),
    signal(
      'contractTests',
      input.hasContractTests,
      2,
      PMA_CHAT_ARCHITECTURE_GOAL.contractTests
    )
  ];
  const totalWeight = signals.reduce((total, item) => total + item.weight, 0);
  const satisfiedWeight = signals
    .filter((item) => item.satisfied)
    .reduce((total, item) => total + item.weight, 0);
  const score = totalWeight === 0 ? 0 : satisfiedWeight / totalWeight;
  return {
    score,
    targetScore: PMA_CHAT_ARCHITECTURE_TARGET_SCORE,
    satisfied: score >= PMA_CHAT_ARCHITECTURE_TARGET_SCORE,
    strengths: signals.filter((item) => item.satisfied).map((item) => item.detail),
    gaps: signals.filter((item) => !item.satisfied).map((item) => item.detail),
    signals
  };
}

export function mergeTranscriptSnapshotWithPendingOptimistic(
  existing: { cardsById: Record<string, ChatTranscriptCard>; order: string[] } | null | undefined,
  backendRows: ChatTranscriptCard[]
): ChatTranscriptCard[] {
  if (!existing) return backendRows;
  const retainedOptimistic = existing.order
    .filter((id) => id.startsWith('optimistic:'))
    .map((id) => existing.cardsById[id])
    .filter((card): card is ChatTranscriptCard => Boolean(card))
    .filter((card) => !transcriptRowsConfirmOptimistic(backendRows, card));
  return [...backendRows, ...retainedOptimistic];
}

export function transcriptRowsConfirmOptimistic(
  backendRows: ChatTranscriptCard[],
  optimistic: ChatTranscriptCard
): boolean {
  if (optimistic.kind !== 'message' || optimistic.message.role !== 'user') return true;
  const optimisticCorrelationId = transcriptCardCorrelationId(optimistic);
  if (!optimisticCorrelationId) return false;
  return backendRows.some((row) => {
    if (row.id.startsWith('optimistic:')) return false;
    if (row.kind !== 'message' || row.message.role !== 'user') return false;
    return transcriptCardCorrelationId(row) === optimisticCorrelationId;
  });
}

export function transcriptCardCorrelationId(card: ChatTranscriptCard): string | null {
  if (card.kind !== 'message') return null;
  const raw = card.message.raw;
  const direct = raw.correlation_id ?? raw.client_turn_id;
  if (typeof direct === 'string' && direct.trim()) return direct.trim();
  const identity = raw.identity;
  if (identity && typeof identity === 'object' && !Array.isArray(identity)) {
    const value = (identity as Record<string, unknown>).correlation_id;
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

export function visibleChatDetailTranscriptCards(
  transcriptCards: ChatTranscriptCard[],
  queuedTurns: PmaQueuedTurn[]
): ChatTranscriptCard[] {
  if (queuedTurns.length === 0) return transcriptCards;
  const queuedTurnIds = new Set(
    queuedTurns.map((turn) => turn.managedTurnId).filter(Boolean)
  );
  if (queuedTurnIds.size === 0) return transcriptCards;
  return transcriptCards.filter(
    (card) => !('turnId' in card && card.turnId && queuedTurnIds.has(card.turnId))
  );
}

export function buildChatDetailDisplayReadModel(
  input: ChatDetailDisplayReadModelInput
): ChatDetailDisplayReadModel {
  const activeCards = visibleChatDetailTranscriptCards(input.transcriptCards, input.queuedTurns);
  const displayTranscriptCards = compactChatTranscriptCards(activeCards);
  const lastAssistantMessageCard = findLastAssistantMessageCard(activeCards);
  const statusBar = buildPmaStatusBar(input.displayedProgress, input.activeChat);
  const streamingMessageId = activeStreamingMessageId(input.displayedProgress, lastAssistantMessageCard);
  const showTypingIndicator = shouldShowTypingIndicator(input.displayedProgress, activeCards);
  const showStatusBar = shouldShowChatDetailStatusBar(statusBar, input.displayedProgress);
  const chatHasActivity = activeCards.length > 0 || showStatusBar;
  const hasRunnableDraft = Boolean(input.activeChat && (input.draft.trim() || input.pendingAttachmentCount > 0));
  const composerWillQueue = shouldQueueComposerDraft(
    input.activeChat,
    input.displayedProgress,
    input.queuedTurns
  );
  return {
    activeCards,
    displayTranscriptCards,
    lastAssistantMessageCard,
    statusBar,
    streamingMessageId,
    showTypingIndicator,
    transcriptListItems: [
      ...displayTranscriptCards.map((card) => ({ kind: 'card' as const, id: card.id, card })),
      ...(showTypingIndicator ? [{ kind: 'typing' as const, id: 'typing-indicator', title: 'Assistant is typing' }] : []),
      ...(input.assistantSharedFileCount > 0 ? [{ kind: 'shared-files' as const, id: 'assistant-shared-files' }] : [])
    ],
    srAnnouncement: screenReaderStreamingAnnouncement(input.displayedProgress, lastAssistantMessageCard),
    showStreamHealthAside: input.streamState === 'connecting' || input.streamState === 'interrupted',
    showStatusBar,
    chatHasActivity,
    showStartPicker: Boolean(input.activeChat) && !input.loadingActive && !input.activeError && !chatHasActivity,
    hasRunnableDraft,
    canInterruptWithDraft: Boolean(
      input.activeChat && input.displayedProgress?.status === 'running' && hasRunnableDraft
    ),
    composerWillQueue,
    queueDepthForCommands: input.queuedTurns.length || input.displayedProgress?.queueDepth || 0
  };
}

function findLastAssistantMessageCard(cards: ChatTranscriptCard[]): ChatTranscriptCard | null {
  for (let i = cards.length - 1; i >= 0; i -= 1) {
    const card = cards[i];
    if (card.kind === 'message' && card.message.role === 'assistant') return card;
  }
  return null;
}

function activeStreamingMessageId(
  progress: PmaRunProgress | null,
  card: ChatTranscriptCard | null
): string | null {
  if (progress?.status !== 'running') return null;
  if (!card || card.kind !== 'message') return null;
  if (card.turnId && progress.id && card.turnId !== progress.id) return null;
  return card.id;
}

function shouldShowTypingIndicator(
  progress: PmaRunProgress | null,
  activeCards: ChatTranscriptCard[]
): boolean {
  if (progress?.status !== 'running') return false;
  const last = activeCards[activeCards.length - 1];
  return Boolean(last && last.kind === 'message' && last.message.role === 'user');
}

function screenReaderStreamingAnnouncement(
  progress: PmaRunProgress | null,
  card: ChatTranscriptCard | null
): string {
  if (progress?.status !== 'running') return '';
  if (!card || card.kind !== 'message') return '';
  if (card.turnId && progress.id && card.turnId !== progress.id) return '';
  const text = (card.message.text ?? '').trim();
  return text.length > 120 ? text.slice(text.length - 120) : text;
}

function shouldShowChatDetailStatusBar(
  statusBar: PmaStatusBar | null,
  progress: PmaRunProgress | null
): boolean {
  if (!statusBar || statusBar.state === 'idle') return false;
  if (statusBar.state === 'done') {
    return Boolean(statusBar.tokenUsageLabel || statusBar.contextRemainingLabel);
  }
  return Boolean(
    (progress?.elapsedSeconds !== null && progress?.elapsedSeconds !== undefined) ||
      (progress?.queueDepth ?? 0) > 0 ||
      statusBar.tokenUsageLabel ||
      statusBar.contextRemainingLabel ||
      ['running', 'waiting', 'blocked', 'failed'].includes(statusBar.state)
  );
}

function shouldQueueComposerDraft(
  activeChat: PmaChatSummary | null,
  progress: PmaRunProgress | null,
  queuedTurns: PmaQueuedTurn[]
): boolean {
  return Boolean(
    activeChat &&
      (progress?.status === 'running' || queuedTurns.length > 0)
  );
}

export function chatTranscriptHasInFlightOptimisticTurn(
  transcript: { order: string[] } | null | undefined
): boolean {
  return Boolean(transcript?.order.some((id) => id.startsWith('optimistic:user:')));
}

export function buildOptimisticUserTranscriptCard(
  chatId: string,
  text: string,
  clientTurnId: string,
  timestamp: string,
  attachments: {
    id: string;
    kind: SurfaceArtifact['kind'];
    title: string;
    url: string | null;
    sizeLabel: string | null;
    uploadedName: string | null;
  }[] = []
): MessageTranscriptCard {
  return {
    kind: 'message',
    id: clientTurnId,
    turnId: null,
    orderKey: `optimistic|${timestamp}|${clientTurnId}`,
    timestamp,
    message: {
      id: clientTurnId,
      chatId,
      role: 'user',
      text,
      createdAt: timestamp,
      status: null,
      artifacts: attachments.map((att) => ({
        id: att.id,
        kind: att.kind,
        title: att.title,
        summary: null,
        url: att.url,
        createdAt: timestamp,
        raw: { size_label: att.sizeLabel, uploadedName: att.uploadedName }
      })),
      raw: {
        optimistic: true,
        client_turn_id: clientTurnId,
        correlation_id: clientTurnId,
        identity: { correlation_id: clientTurnId }
      }
    }
  };
}

export function isOptimisticQueuedTurn(turn: PmaQueuedTurn): boolean {
  return turn.managedTurnId.startsWith('optimistic-queue:');
}

export function buildOptimisticQueuedTurn(
  text: string,
  clientTurnId: string,
  position: number,
  enqueuedAt: string
): PmaQueuedTurn {
  return {
    managedTurnId: `optimistic-queue:${clientTurnId}`,
    position,
    state: 'queueing',
    prompt: text,
    promptPreview: text,
    attachments: [],
    model: null,
    reasoning: null,
    enqueuedAt,
    raw: { optimistic: true }
  };
}

export function withoutOptimisticQueuedTurn(
  queuedTurns: PmaQueuedTurn[],
  clientTurnId: string
): PmaQueuedTurn[] {
  const id = `optimistic-queue:${clientTurnId}`;
  return queuedTurns.filter((turn) => turn.managedTurnId !== id);
}

export function queueContainsCommittedClientTurn(
  queuedTurns: PmaQueuedTurn[],
  clientTurnId: string
): boolean {
  return queuedTurns.some((turn) => {
    if (isOptimisticQueuedTurn(turn)) return false;
    const raw = turn.raw as { client_turn_id?: unknown } | null | undefined;
    return typeof raw?.client_turn_id === 'string' && raw.client_turn_id === clientTurnId;
  });
}

export function transcriptContainsCommittedUserRow(
  transcript: { cardsById: Record<string, ChatTranscriptCard>; order: string[] } | null | undefined,
  clientTurnId: string
): boolean {
  if (!transcript) return false;
  return transcript.order.some((id) => {
    if (id.startsWith('optimistic:')) return false;
    const card = transcript.cardsById[id];
    if (!card || card.kind !== 'message' || card.message.role !== 'user') return false;
    return transcriptCardCorrelationId(card) === clientTurnId;
  });
}

export function progressElapsedWithLiveWall(value: PmaRunProgress, nowMs: number): number {
  const base = value.elapsedSeconds ?? 0;
  if (value.status !== 'running' || !value.startedAt) return base;
  const startedMs = Date.parse(value.startedAt);
  if (!Number.isFinite(startedMs)) return base;
  const wallElapsed = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  return Math.max(base, wallElapsed);
}

export function progressWithLiveElapsed(value: PmaRunProgress | null, nowMs: number): PmaRunProgress | null {
  if (!value) return value;
  const elapsedSeconds = progressElapsedWithLiveWall(value, nowMs);
  return elapsedSeconds === value.elapsedSeconds ? value : { ...value, elapsedSeconds };
}

export function mergePmaProgressUpdate(
  previousProgress: PmaRunProgress | null,
  nextProgress: PmaRunProgress,
  nowMs: number
): PmaRunProgress {
  if (!previousProgress || previousProgress.id !== nextProgress.id) return nextProgress;
  const incomingElapsed = nextProgress.elapsedSeconds ?? 0;
  const mergedElapsed = Math.max(progressElapsedWithLiveWall(previousProgress, nowMs), incomingElapsed);
  const seen = new Map<string, number>();
  const events: SurfaceArtifact[] = [];
  for (const ev of [...previousProgress.events, ...nextProgress.events]) {
    const key = canonicalProgressEventKey(ev);
    if (!key) continue;
    const existingIndex = seen.get(key);
    if (existingIndex !== undefined) {
      events[existingIndex] = { ...events[existingIndex], ...ev, raw: { ...events[existingIndex].raw, ...ev.raw } };
      continue;
    }
    seen.set(key, events.length);
    events.push(ev);
  }
  return {
    ...nextProgress,
    startedAt: nextProgress.startedAt ?? previousProgress.startedAt,
    elapsedSeconds: mergedElapsed,
    events
  };
}

function canonicalProgressEventKey(event: SurfaceArtifact): string {
  const raw = event.raw && typeof event.raw === 'object' && !Array.isArray(event.raw) ? event.raw : {};
  const progressItem = raw.progress_item && typeof raw.progress_item === 'object' && !Array.isArray(raw.progress_item)
    ? raw.progress_item as Record<string, unknown>
    : {};
  return String(progressItem.item_id ?? raw.progress_item_id ?? event.id ?? '');
}

function signal(
  principle: PmaChatArchitecturePrinciple,
  satisfied: boolean,
  weight: number,
  detail: string
): PmaChatArchitectureSignal {
  return { principle, satisfied, weight, detail };
}
