import type { ChatQueuedTurn } from '$lib/api/client';
import type { ArtifactDelivery, ChatSummary, ChatRunProgress } from '$lib/viewModels/domain';
import {
  buildChatActivityCards,
  buildChatStatusBar,
  compactChatTranscriptCards,
  type ChatTranscriptCard,
  type ChatStatusBar
} from '$lib/viewModels/chat';
import type { ChatDetailStreamState } from './chatDetailLiveProjection';

export type ChatTranscriptListItem =
  | { kind: 'card'; id: string; card: ChatTranscriptCard }
  | { kind: 'typing'; id: string; title: string }
  | { kind: 'shared-files'; id: string; files: ArtifactDelivery[] }
  | { kind: 'tail-spacer'; id: string };

export type ChatDetailDisplayReadModelInput = {
  transcriptCards: ChatTranscriptCard[];
  queuedTurns: ChatQueuedTurn[];
  displayedProgress: ChatRunProgress | null;
  activeChat: ChatSummary | null;
  assistantSharedFiles: ArtifactDelivery[];
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
  statusBar: ChatStatusBar | null;
  streamingMessageId: string | null;
  showTypingIndicator: boolean;
  transcriptListItems: ChatTranscriptListItem[];
  statusAnnouncement: string;
  alertAnnouncement: string;
  showStreamHealthAside: boolean;
  showStatusBar: boolean;
  chatHasActivity: boolean;
  showStartPicker: boolean;
  hasRunnableDraft: boolean;
  canInterruptWithDraft: boolean;
  canStopRun: boolean;
  composerWillQueue: boolean;
  queueDepthForCommands: number;
  runActive: boolean;
};

export function visibleChatDetailTranscriptCards(
  transcriptCards: ChatTranscriptCard[],
  queuedTurns: ChatQueuedTurn[]
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
  const liveActivityCards = buildLiveProgressTranscriptCards(input.displayedProgress, activeCards);
  const displayTranscriptCards = compactChatTranscriptCards(
    mergeTranscriptCardsWithLiveActivity(activeCards, liveActivityCards, input.displayedProgress?.id ?? null)
  );
  const lastAssistantMessageCard = findLastAssistantMessageCard(activeCards);
  const statusBar = buildChatStatusBar(input.displayedProgress, input.activeChat);
  const streamingMessageId = activeStreamingMessageId(input.displayedProgress, lastAssistantMessageCard);
  const showTypingIndicator = shouldShowTypingIndicator(input.displayedProgress, activeCards);
  const showStatusBar = shouldShowChatDetailStatusBar(statusBar, input.displayedProgress, activeCards);
  const sharedFileItems = buildSharedFileListItems(displayTranscriptCards, input.assistantSharedFiles);
  const chatHasActivity = activeCards.length > 0 || sharedFileItems.length > 0 || showStatusBar;
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
      ...mergeTranscriptCardsWithSharedFiles(displayTranscriptCards, sharedFileItems),
      ...(showTypingIndicator ? [{ kind: 'typing' as const, id: 'typing-indicator', title: 'Assistant is typing' }] : []),
      ...(showStatusBar ? [{ kind: 'tail-spacer' as const, id: 'status-bar-tail-spacer' }] : [])
    ],
    statusAnnouncement: screenReaderStatusAnnouncement(input, statusBar, lastAssistantMessageCard),
    alertAnnouncement: screenReaderAlertAnnouncement(statusBar),
    showStreamHealthAside: input.streamState === 'connecting' || input.streamState === 'interrupted',
    showStatusBar,
    chatHasActivity,
    showStartPicker: Boolean(input.activeChat) && !input.loadingActive && !input.activeError && !chatHasActivity,
    hasRunnableDraft,
    canInterruptWithDraft: Boolean(
      input.activeChat && input.displayedProgress?.status === 'running' && hasRunnableDraft
    ),
    canStopRun: Boolean(
      input.activeChat && input.displayedProgress?.status === 'running' && !hasRunnableDraft
    ),
    composerWillQueue,
    queueDepthForCommands: input.queuedTurns.length || input.displayedProgress?.queueDepth || 0,
    runActive: isRunActiveForToolDisplay(input.displayedProgress, input.activeChat, activeCards)
  };
}

function mergeTranscriptCardsWithSharedFiles(
  cards: ChatTranscriptCard[],
  sharedFileItems: Array<{ afterIndex: number; item: Extract<ChatTranscriptListItem, { kind: 'shared-files' }> }>
): ChatTranscriptListItem[] {
  const items: ChatTranscriptListItem[] = [];
  for (const pending of sharedFileItems.filter((entry) => entry.afterIndex < 0)) {
    items.push(pending.item);
  }
  cards.forEach((card, index) => {
    items.push({ kind: 'card', id: card.id, card });
    for (const pending of sharedFileItems.filter((entry) => entry.afterIndex === index)) {
      items.push(pending.item);
    }
  });
  return items;
}

function buildSharedFileListItems(
  cards: ChatTranscriptCard[],
  files: ArtifactDelivery[]
): Array<{ afterIndex: number; item: Extract<ChatTranscriptListItem, { kind: 'shared-files' }> }> {
  if (files.length === 0) return [];
  const grouped = new Map<number, ArtifactDelivery[]>();
  for (const file of files) {
    const afterIndex = transcriptInsertionIndex(cards, deliveryTimestamp(file));
    grouped.set(afterIndex, [...(grouped.get(afterIndex) ?? []), file]);
  }
  return [...grouped.entries()]
    .sort(([left], [right]) => left - right)
    .map(([afterIndex, groupedFiles]) => ({
      afterIndex,
      item: {
        kind: 'shared-files' as const,
        id: `assistant-shared-files:${afterIndex}:${groupedFiles.map((file) => file.deliveryId).join(':')}`,
        files: groupedFiles
      }
    }));
}

function transcriptInsertionIndex(cards: ChatTranscriptCard[], timestamp: number): number {
  if (cards.length === 0) return -1;
  if (!Number.isFinite(timestamp) || timestamp <= 0) return cards.length - 1;
  let index = -1;
  for (let i = 0; i < cards.length; i += 1) {
    const cardTime = cardTimestamp(cards[i]);
    if (Number.isFinite(cardTime) && cardTime > 0 && cardTime <= timestamp) index = i;
  }
  return index >= 0 ? index : -1;
}

function deliveryTimestamp(file: ArtifactDelivery): number {
  return firstValidTimestamp(file.sentAt, file.updatedAt, file.createdAt, file.failedAt);
}

function cardTimestamp(card: ChatTranscriptCard): number {
  if (card.kind === 'message') return firstValidTimestamp(card.message.createdAt, card.timestamp);
  if ('timestamp' in card) return firstValidTimestamp(card.timestamp);
  return 0;
}

function firstValidTimestamp(...values: Array<string | null | undefined>): number {
  for (const value of values) {
    if (!value) continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return 0;
}

function isRunActiveForToolDisplay(
  progress: ChatRunProgress | null,
  chat: ChatSummary | null,
  cards: ChatTranscriptCard[]
): boolean {
  if (progress?.status === 'running' && !progress.terminal) return true;
  if (progress?.terminal || progress?.status === 'done' || progress?.status === 'failed') return false;
  if (!transcriptHasStartedTool(cards)) return false;
  const status = progress?.status ?? chat?.status;
  if (status === 'done' || status === 'failed' || status === 'idle') return false;
  if (status === 'running' || status === 'waiting' || status === 'blocked') return true;
  return !progress && !chat;
}

function transcriptHasStartedTool(cards: ChatTranscriptCard[]): boolean {
  for (const card of cards) {
    if (card.kind === 'tool_group' && card.tools.some((tool) => tool.state === 'started')) return true;
    if (card.kind === 'turn_summary' && transcriptHasStartedTool(card.cards)) return true;
  }
  return false;
}

function buildLiveProgressTranscriptCards(
  progress: ChatRunProgress | null,
  transcriptCards: ChatTranscriptCard[]
): ChatTranscriptCard[] {
  if (!progress?.events.length) return [];
  const existingIds = transcriptActivityIdentitySet(transcriptCards);
  return buildChatActivityCards(progress.events, { fallbackTurnId: progress.id }).filter((card) => {
    const ids = transcriptCardActivityIds(card);
    if (ids.length === 0) return !existingIds.has(card.id);
    return !ids.some((id) => existingIds.has(id));
  });
}

function mergeTranscriptCardsWithLiveActivity(
  transcriptCards: ChatTranscriptCard[],
  liveActivityCards: ChatTranscriptCard[],
  managedTurnId: string | null
): ChatTranscriptCard[] {
  if (liveActivityCards.length === 0) return transcriptCards;
  if (!managedTurnId) return [...transcriptCards, ...liveActivityCards];
  const merged: ChatTranscriptCard[] = [];
  let inserted = false;
  for (const card of transcriptCards) {
    if (!inserted && isAssistantMessageForTurn(card, managedTurnId)) {
      merged.push(...liveActivityCards);
      inserted = true;
    }
    merged.push(card);
    if (!inserted && isUserMessageForTurn(card, managedTurnId)) {
      merged.push(...liveActivityCards);
      inserted = true;
    }
  }
  if (!inserted) merged.push(...liveActivityCards);
  return merged;
}

function isUserMessageForTurn(card: ChatTranscriptCard, managedTurnId: string): boolean {
  return card.kind === 'message' && card.turnId === managedTurnId && card.message.role === 'user';
}

function isAssistantMessageForTurn(card: ChatTranscriptCard, managedTurnId: string): boolean {
  return card.kind === 'message' && card.turnId === managedTurnId && card.message.role === 'assistant';
}

function transcriptActivityIdentitySet(cards: ChatTranscriptCard[]): Set<string> {
  const ids = new Set<string>();
  for (const card of cards) {
    ids.add(card.id);
    for (const id of transcriptCardActivityIds(card)) ids.add(id);
  }
  return ids;
}

function transcriptCardActivityIds(card: ChatTranscriptCard): string[] {
  if (card.kind === 'intermediate') return uniqueNonEmptyStrings([...card.eventIds, ...card.progressSourceIds]);
  if (card.kind === 'tool_group') {
    return uniqueNonEmptyStrings(card.tools.flatMap((tool) => tool.eventIds));
  }
  return [];
}

function uniqueNonEmptyStrings(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

function findLastAssistantMessageCard(cards: ChatTranscriptCard[]): ChatTranscriptCard | null {
  for (let i = cards.length - 1; i >= 0; i -= 1) {
    const card = cards[i];
    if (card.kind === 'message' && card.message.role === 'assistant') return card;
  }
  return null;
}

function activeStreamingMessageId(
  progress: ChatRunProgress | null,
  card: ChatTranscriptCard | null
): string | null {
  if (progress?.status !== 'running') return null;
  if (!card || card.kind !== 'message') return null;
  if (card.turnId && progress.id && card.turnId !== progress.id) return null;
  return card.id;
}

function shouldShowTypingIndicator(
  progress: ChatRunProgress | null,
  activeCards: ChatTranscriptCard[]
): boolean {
  if (progress?.status !== 'running') return false;
  const last = activeCards[activeCards.length - 1];
  return Boolean(last && last.kind === 'message' && last.message.role === 'user');
}

function screenReaderStatusAnnouncement(
  input: ChatDetailDisplayReadModelInput,
  statusBar: ChatStatusBar | null,
  card: ChatTranscriptCard | null
): string {
  const queueDepth = input.queuedTurns.length || input.displayedProgress?.queueDepth || 0;
  const queueText = queueDepth > 0
    ? `${queueDepth} queued message${queueDepth === 1 ? '' : 's'}`
    : '';
  if (statusBar?.state === 'running') {
    return ['Assistant is responding', queueText].filter(Boolean).join('. ');
  }
  if (statusBar?.state === 'waiting') {
    return ['Assistant is waiting', queueText].filter(Boolean).join('. ');
  }
  if (statusBar?.state === 'blocked') {
    return ['Assistant needs attention', queueText].filter(Boolean).join('. ');
  }
  if (statusBar?.state === 'done') {
    const hasCurrentAssistantReply = Boolean(
      card &&
        card.kind === 'message' &&
        (!card.turnId || !input.displayedProgress?.id || card.turnId === input.displayedProgress.id)
    );
    return [hasCurrentAssistantReply ? 'Assistant replied' : 'Turn completed', queueText].filter(Boolean).join('. ');
  }
  if (input.streamState === 'connecting') return 'Connecting to live chat updates';
  return queueText;
}

function screenReaderAlertAnnouncement(statusBar: ChatStatusBar | null): string {
  if (statusBar?.state === 'failed') {
    return statusBar.phase ? `Turn failed: ${statusBar.phase}` : 'Turn failed';
  }
  return '';
}

function shouldShowChatDetailStatusBar(
  statusBar: ChatStatusBar | null,
  progress: ChatRunProgress | null,
  activeCards: ChatTranscriptCard[]
): boolean {
  if (!statusBar) return false;
  if (statusBar.state === 'idle') return activeCards.length > 0;
  if (statusBar.state === 'done') {
    return activeCards.length > 0 || Boolean(statusBar.tokenUsageLabel || statusBar.contextRemainingLabel);
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
  activeChat: ChatSummary | null,
  progress: ChatRunProgress | null,
  queuedTurns: ChatQueuedTurn[]
): boolean {
  return Boolean(
    activeChat &&
      (progress?.status === 'running' || queuedTurns.length > 0)
  );
}
