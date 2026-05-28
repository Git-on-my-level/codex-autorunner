import { describe, expect, it } from 'vitest';
import type { PmaQueuedTurn } from '$lib/api/client';
import type { PmaChatSummary, PmaRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';
import {
  buildChatDetailDisplayReadModel,
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

  it('builds the chat detail transcript and composer read model outside the page', () => {
    const user = { ...userCard('turn:run-1:user', 'next task', {}), turnId: 'run-1' };
    const assistant = { ...assistantCard('turn:run-1:assistant', 'working'), turnId: 'run-1' };
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [user, assistant],
      queuedTurns: [queuedTurn('queued-turn')],
      displayedProgress: { ...progress('run-1', 12, []), queueDepth: 1 },
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 1,
      streamState: 'connecting',
      loadingActive: false,
      activeError: null,
      draft: 'follow up',
      pendingAttachmentCount: 0
    });

    expect(model.displayTranscriptCards.map((card) => card.id)).toEqual([
      'turn:run-1:user',
      'turn:run-1:assistant'
    ]);
    expect(model.streamingMessageId).toBe('turn:run-1:assistant');
    expect(model.transcriptListItems.map((item) => item.kind)).toEqual(['card', 'card', 'shared-files']);
    expect(model.statusAnnouncement).toBe('Assistant is responding. 1 queued message');
    expect(model.alertAnnouncement).toBe('');
    expect(model.showStreamHealthAside).toBe(true);
    expect(model.showStatusBar).toBe(true);
    expect(model.chatHasActivity).toBe(true);
    expect(model.showStartPicker).toBe(false);
    expect(model.hasRunnableDraft).toBe(true);
    expect(model.canInterruptWithDraft).toBe(true);
    expect(model.composerWillQueue).toBe(true);
    expect(model.queueDepthForCommands).toBe(1);
  });

  it('does not mark a stale assistant card as streaming for a newer running turn', () => {
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [{ ...assistantCard('turn:old:assistant', 'done'), turnId: 'old-turn' }],
      queuedTurns: [],
      displayedProgress: progress('new-turn', 3, []),
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.streamingMessageId).toBeNull();
    expect(model.statusAnnouncement).toBe('Assistant is responding');
    expect(model.alertAnnouncement).toBe('');
  });

  it('does not duplicate visible assertive alerts in the sr-only alert region', () => {
    const activeErrorModel = buildChatDetailDisplayReadModel({
      transcriptCards: [],
      queuedTurns: [],
      displayedProgress: null,
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: new Error('backend unavailable'),
      draft: '',
      pendingAttachmentCount: 0
    });
    const interruptedModel = buildChatDetailDisplayReadModel({
      transcriptCards: [],
      queuedTurns: [],
      displayedProgress: null,
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'interrupted',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });
    const failedProgress = { ...progress('run-1', 3, []), status: 'failed' as const, phase: 'tool_error' };
    const failedModel = buildChatDetailDisplayReadModel({
      transcriptCards: [],
      queuedTurns: [],
      displayedProgress: failedProgress,
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(activeErrorModel.alertAnnouncement).toBe('');
    expect(interruptedModel.alertAnnouncement).toBe('');
    expect(failedModel.alertAnnouncement).toBe('Turn failed: tool error');
  });

  it('shows the start picker only for an idle selected chat with no projected activity', () => {
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [],
      queuedTurns: [],
      displayedProgress: null,
      activeChat: { ...chatSummary('chat-1'), status: 'idle' },
      assistantSharedFileCount: 0,
      streamState: 'idle',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.showStatusBar).toBe(false);
    expect(model.chatHasActivity).toBe(false);
    expect(model.showStartPicker).toBe(true);
    expect(model.hasRunnableDraft).toBe(false);
    expect(model.composerWillQueue).toBe(false);
  });

  it('merges live progress elapsed time and deduplicates event rows', () => {
    const previous = progress('run-1', 10, [artifact('event-1')]);
    const next = progress('run-1', 5, [artifact('event-1'), artifact('event-2')]);

    const merged = mergePmaProgressUpdate(previous, next, Date.parse(now) + 20_000);

    expect(merged.elapsedSeconds).toBe(20);
    expect(merged.events.map((event) => event.id)).toEqual(['event-1', 'event-2']);
  });

  it('replaces live progress events by canonical progress item id', () => {
    const previous = progress('run-1', 10, [
      artifact('event-1', { item_id: 'progress:assistant_update:0001', summary: 'Read' })
    ]);
    const next = progress('run-1', 11, [
      artifact('event-2', { item_id: 'progress:assistant_update:0001', summary: 'Reading files' })
    ]);

    const merged = mergePmaProgressUpdate(previous, next, Date.parse(now) + 11_000);

    expect(merged.events).toHaveLength(1);
    expect(merged.events[0].id).toBe('event-2');
    expect(merged.events[0].raw.progress_item).toMatchObject({
      item_id: 'progress:assistant_update:0001',
      summary: 'Reading files'
    });
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

function chatSummary(id: string): PmaChatSummary {
  return {
    id,
    title: 'Chat One',
    lifecycleStatus: 'active',
    status: 'running',
    agentId: 'codex',
    agentProfile: null,
    model: 'gpt-5',
    repoId: null,
    worktreeId: null,
    ticketId: null,
    ticketDone: null,
    ticketPath: null,
    runId: null,
    unreadCount: 0,
    flowType: null,
    isTicketFlow: false,
    progressPercent: null,
    updatedAt: now,
    raw: {}
  };
}

function artifact(id: string, progressItem: Record<string, unknown> = {}): SurfaceArtifact {
  return {
    id,
    kind: 'progress',
    title: id,
    summary: null,
    url: null,
    createdAt: now,
    raw: Object.keys(progressItem).length ? { progress_item: progressItem } : {}
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
