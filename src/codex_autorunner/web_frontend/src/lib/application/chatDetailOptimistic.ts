import type { ChatQueuedTurn } from '$lib/api/client';
import type { SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';

type MessageTranscriptCard = Extract<ChatTranscriptCard, { kind: 'message' }>;

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

export function isOptimisticQueuedTurn(turn: ChatQueuedTurn): boolean {
  return turn.managedTurnId.startsWith('optimistic-queue:');
}

export function buildOptimisticQueuedTurn(
  text: string,
  clientTurnId: string,
  position: number,
  enqueuedAt: string
): ChatQueuedTurn {
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
  queuedTurns: ChatQueuedTurn[],
  clientTurnId: string
): ChatQueuedTurn[] {
  const id = `optimistic-queue:${clientTurnId}`;
  return queuedTurns.filter((turn) => turn.managedTurnId !== id);
}

export function queueContainsCommittedClientTurn(
  queuedTurns: ChatQueuedTurn[],
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
