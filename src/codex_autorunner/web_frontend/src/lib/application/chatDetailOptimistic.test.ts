import { describe, expect, it } from 'vitest';
import type { ChatQueuedTurn } from '$lib/api/client';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';
import {
  buildOptimisticQueuedTurn,
  buildOptimisticUserTranscriptCard,
  mergeTranscriptSnapshotWithPendingOptimistic,
  queueContainsCommittedClientTurn,
  transcriptCardCorrelationId,
  transcriptContainsCommittedUserRow,
  transcriptRowsConfirmOptimistic,
  withoutOptimisticQueuedTurn
} from './chatDetailOptimistic';

const now = '2026-05-16T12:00:00.000Z';
type MessageTranscriptCard = Extract<ChatTranscriptCard, { kind: 'message' }>;

describe('chat transcript optimistic reconciliation', () => {
  it('retains an optimistic user row until a backend row confirms its correlation id', () => {
    const optimistic = userCard('optimistic:user:1', 'draft text', {
      optimistic: true,
      client_turn_id: 'client-1'
    });
    const backendAssistant = assistantCard('turn:1:assistant', 'Working on it');

    expect(
      mergeTranscriptSnapshotWithPendingOptimistic(
        { cardsById: { [optimistic.id]: optimistic }, order: [optimistic.id] },
        [backendAssistant]
      ).map((card) => card.id)
    ).toEqual(['turn:1:assistant', 'optimistic:user:1']);
  });

  it('drops the optimistic row after the backend returns the matching user row', () => {
    const optimistic = userCard('optimistic:user:1', 'draft text', {
      optimistic: true,
      client_turn_id: 'client-1'
    });
    const backendUser = userCard('turn:1:user', 'draft text', {
      identity: { correlation_id: 'client-1' }
    });

    expect(transcriptRowsConfirmOptimistic([backendUser], optimistic)).toBe(true);
    expect(
      mergeTranscriptSnapshotWithPendingOptimistic(
        { cardsById: { [optimistic.id]: optimistic }, order: [optimistic.id] },
        [backendUser]
      ).map((card) => card.id)
    ).toEqual(['turn:1:user']);
  });

  it('reads correlation ids from direct fields before nested identity metadata', () => {
    expect(
      transcriptCardCorrelationId(
        userCard('turn:1:user', 'hello', {
          correlation_id: 'direct-id',
          identity: { correlation_id: 'nested-id' }
        })
      )
    ).toBe('direct-id');
    expect(
      transcriptCardCorrelationId(
        userCard('turn:2:user', 'hello', {
          identity: { correlation_id: 'nested-id' }
        })
      )
    ).toBe('nested-id');
  });

  it('builds and removes optimistic queue rows', () => {
    const optimistic = buildOptimisticQueuedTurn('second message', 'client-1', 2, now);
    const committed = queuedTurn('turn-2', { client_turn_id: 'client-1' });
    const queue = [queuedTurn('turn-1'), optimistic, committed];

    expect(optimistic).toMatchObject({
      managedTurnId: 'optimistic-queue:client-1',
      position: 2,
      state: 'queueing',
      promptPreview: 'second message'
    });
    expect(withoutOptimisticQueuedTurn(queue, 'client-1').map((turn) => turn.managedTurnId)).toEqual([
      'turn-1',
      'turn-2'
    ]);
    expect(queueContainsCommittedClientTurn(queue, 'client-1')).toBe(true);
  });

  it('builds optimistic user rows and detects backend confirmation by correlation id', () => {
    const optimistic = buildOptimisticUserTranscriptCard('chat-1', 'hello', 'client-1', now, [
      {
        id: 'att-1',
        kind: 'file',
        title: 'notes.md',
        url: null,
        sizeLabel: '12 B',
        uploadedName: 'notes.md'
      }
    ]);
    const backendUser = userCard('turn:1:user', 'hello', {
      identity: { correlation_id: 'client-1' }
    });

    expect(optimistic.message.artifacts[0]).toMatchObject({ id: 'att-1', kind: 'file' });
    expect(
      transcriptContainsCommittedUserRow(
        { cardsById: { [backendUser.id]: backendUser }, order: [backendUser.id] },
        'client-1'
      )
    ).toBe(true);
  });
});

function userCard(id: string, text: string, raw: Record<string, unknown>): MessageTranscriptCard {
  return {
    kind: 'message',
    id,
    turnId: id.startsWith('turn:') ? '1' : null,
    orderKey: id.startsWith('optimistic:') ? `optimistic|${now}|${id}` : `00000001|${now}|${id}`,
    timestamp: now,
    message: {
      id,
      chatId: 'chat-1',
      role: 'user',
      text,
      createdAt: now,
      status: null,
      artifacts: [],
      raw
    }
  };
}

function assistantCard(id: string, text: string): MessageTranscriptCard {
  const base = userCard(id, text, {});
  return {
    ...base,
    message: {
      ...base.message,
      role: 'assistant'
    }
  };
}

function queuedTurn(managedTurnId: string, raw: Record<string, unknown> = {}): ChatQueuedTurn {
  return {
    managedTurnId,
    position: 1,
    state: 'queued',
    prompt: 'queued prompt',
    promptPreview: 'queued prompt',
    attachments: [],
    model: null,
    reasoning: null,
    enqueuedAt: now,
    raw
  };
}
