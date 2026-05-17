import { describe, expect, it } from 'vitest';
import type { PmaQueuedTurn } from '$lib/api/client';
import type { PmaRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';
import {
  buildOptimisticQueuedTurn,
  buildOptimisticUserTranscriptCard,
  evaluatePmaChatArchitectureGoal,
  mergePmaProgressUpdate,
  mergeTranscriptSnapshotWithPendingOptimistic,
  queueContainsCommittedClientTurn,
  transcriptCardCorrelationId,
  transcriptContainsCommittedUserRow,
  transcriptRowsConfirmOptimistic,
  visibleChatDetailTranscriptCards,
  withoutOptimisticQueuedTurn
} from './pmaChatArchitecture';

const now = '2026-05-16T12:00:00.000Z';
type MessageTranscriptCard = Extract<ChatTranscriptCard, { kind: 'message' }>;

describe('PMA chat architecture goal', () => {
  it('treats backend projection plus application boundaries as the target architecture', () => {
    const result = evaluatePmaChatArchitectureGoal({
      transcriptProjectionIsBackendOwned: true,
      routeOwnsTranscriptReconciliation: false,
      commandsUseApplicationPlans: true,
      capabilitiesHaveTypedBoundaries: true,
      usesCursorStreamRepair: true,
      usesUnboundedRendering: false,
      hasContractTests: true
    });

    expect(result.satisfied).toBe(true);
    expect(result.score).toBe(1);
    expect(result.gaps).toEqual([]);
  });

  it('penalizes page-owned orchestration and unbounded rendering even when commands are typed', () => {
    const result = evaluatePmaChatArchitectureGoal({
      transcriptProjectionIsBackendOwned: true,
      routeOwnsTranscriptReconciliation: true,
      commandsUseApplicationPlans: true,
      capabilitiesHaveTypedBoundaries: false,
      usesCursorStreamRepair: true,
      usesUnboundedRendering: true,
      hasContractTests: true
    });

    expect(result.satisfied).toBe(false);
    expect(result.gaps).toContain(
      'Keep Svelte routes focused on binding UI controls to application services, not owning transcript or stream reconciliation rules.'
    );
    expect(result.gaps).toContain(
      'Keep chat indexes and transcripts bounded or virtualized so large workspaces do not degrade page behavior.'
    );
  });
});

describe('PMA chat transcript optimistic reconciliation', () => {
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
});

describe('PMA chat detail state composition', () => {
  it('hides backend-persisted queued user rows from the active transcript', () => {
    const queuedUser = userCard('turn:queued:user', 'queued', {});
    const visibleAssistant = assistantCard('turn:active:assistant', 'hello');
    const cards = visibleChatDetailTranscriptCards(
      [
        { ...queuedUser, turnId: 'queued-turn' },
        { ...visibleAssistant, turnId: 'active-turn' }
      ],
      [queuedTurn('queued-turn')]
    );

    expect(cards.map((card) => card.id)).toEqual(['turn:active:assistant']);
  });

  it('builds and removes optimistic queue rows through the detail view model', () => {
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

  it('merges live progress elapsed time and deduplicates event rows', () => {
    const previous = progress('run-1', 10, [artifact('event-1')]);
    const next = progress('run-1', 5, [artifact('event-1'), artifact('event-2')]);

    const merged = mergePmaProgressUpdate(previous, next, Date.parse(now) + 20_000);

    expect(merged.elapsedSeconds).toBe(20);
    expect(merged.events.map((event) => event.id)).toEqual(['event-1', 'event-2']);
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

function queuedTurn(managedTurnId: string, raw: Record<string, unknown> = {}): PmaQueuedTurn {
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

function artifact(id: string): SurfaceArtifact {
  return {
    id,
    kind: 'progress',
    title: id,
    summary: null,
    url: null,
    createdAt: now,
    raw: {}
  };
}

function progress(id: string, elapsedSeconds: number, events: SurfaceArtifact[]): PmaRunProgress {
  return {
    id,
    chatId: 'chat-1',
    status: 'running',
    workStatus: 'running',
    operatorStatus: 'running',
    terminal: false,
    streamShouldClose: false,
    streamCloseReason: null,
    phase: null,
    guidance: null,
    queueDepth: 0,
    elapsedSeconds,
    startedAt: now,
    idleSeconds: null,
    lastEventId: null,
    lastEventAt: null,
    progressPercent: null,
    events,
    raw: {}
  };
}
