import { describe, expect, it } from 'vitest';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';
import {
  evaluatePmaChatArchitectureGoal,
  mergeTranscriptSnapshotWithPendingOptimistic,
  transcriptCardCorrelationId,
  transcriptRowsConfirmOptimistic
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
