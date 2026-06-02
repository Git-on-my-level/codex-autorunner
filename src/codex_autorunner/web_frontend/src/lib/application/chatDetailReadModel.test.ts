import { describe, expect, it } from 'vitest';
import type { ChatQueuedTurn } from '$lib/api/client';
import type { ChatSummary, ChatRunProgress, SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';
import {
  buildChatDetailDisplayReadModel,
  visibleChatDetailTranscriptCards
} from './chatDetailReadModel';

const now = '2026-05-16T12:00:00.000Z';
type MessageTranscriptCard = Extract<ChatTranscriptCard, { kind: 'message' }>;

describe('chat detail state composition', () => {
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
    expect(model.transcriptListItems.map((item) => item.kind)).toEqual(['card', 'card', 'shared-files', 'tail-spacer']);
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

  it('renders live commentary progress in the transcript for the active turn', () => {
    const user = { ...userCard('turn:run-1:user', 'next task', {}), turnId: 'run-1' };
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [user],
      queuedTurns: [],
      displayedProgress: progress('run-1', 12, [
        artifact('event-1', {
          item_id: 'progress:commentary:0001',
          kind: 'assistant_update',
          title: 'commentary',
          summary: 'I am checking the renderer.',
          event_ids: ['journal:1']
        })
      ]),
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.displayTranscriptCards.map((card) => card.kind)).toEqual(['message', 'intermediate']);
    expect(model.displayTranscriptCards[1]).toMatchObject({
      kind: 'intermediate',
      title: 'commentary',
      text: 'I am checking the renderer.',
      turnId: 'run-1'
    });
    expect(model.transcriptListItems.map((item) => item.kind)).toEqual(['card', 'card', 'typing', 'tail-spacer']);
  });

  it('keeps terminal live commentary before the final assistant reply', () => {
    const user = { ...userCard('turn:run-1:user', 'next task', {}), turnId: 'run-1' };
    const assistant = { ...assistantCard('turn:run-1:assistant', 'done'), turnId: 'run-1' };
    const terminalProgress = {
      ...progress('run-1', 12, [
        artifact('event-1', {
          item_id: 'progress:commentary:0001',
          kind: 'assistant_update',
          title: 'commentary',
          summary: 'I found the old path and restored it.',
          event_ids: ['journal:1']
        })
      ]),
      status: 'done' as const,
      workStatus: 'done' as const,
      operatorStatus: 'done' as const,
      terminal: true
    };
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [user, assistant],
      queuedTurns: [],
      displayedProgress: terminalProgress,
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.displayTranscriptCards.map((card) => `${card.kind}:${card.id}`)).toEqual([
      'message:turn:run-1:user',
      'intermediate:intermediate-event-1',
      'message:turn:run-1:assistant'
    ]);
    expect(model.transcriptListItems.map((item) => item.kind)).toEqual(['card', 'card', 'card', 'tail-spacer']);
  });

  it('does not duplicate live progress already projected by the backend transcript', () => {
    const user = { ...userCard('turn:run-1:user', 'next task', {}), turnId: 'run-1' };
    const commentary: ChatTranscriptCard = {
      kind: 'intermediate',
      id: 'timeline:commentary:1',
      title: 'commentary',
      text: 'I am checking the renderer.',
      eventIds: ['journal:1'],
      progressSourceIds: ['progress:commentary:0001'],
      detail: null,
      turnId: 'run-1',
      orderKey: '002',
      timestamp: now
    };
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [user, commentary],
      queuedTurns: [],
      displayedProgress: progress('run-1', 12, [
        artifact('event-1', {
          item_id: 'progress:commentary:0001',
          kind: 'assistant_update',
          title: 'commentary',
          summary: 'I am checking the renderer.',
          event_ids: ['journal:1']
        })
      ]),
      activeChat: chatSummary('chat-1'),
      assistantSharedFileCount: 0,
      streamState: 'connected',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.displayTranscriptCards.filter((card) => card.kind === 'intermediate')).toHaveLength(1);
    expect(model.displayTranscriptCards.map((card) => card.id)).toEqual(['turn:run-1:user', 'timeline:commentary:1']);
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

  it('keeps the status bar visible for idle chats with transcript history', () => {
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [assistantCard('turn:old:assistant', 'done')],
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

    expect(model.showStatusBar).toBe(true);
    expect(model.statusBar?.state).toBe('idle');
    expect(model.transcriptListItems.at(-1)?.kind).toBe('tail-spacer');
    expect(model.chatHasActivity).toBe(true);
    expect(model.showStartPicker).toBe(false);
  });

  it('keeps the status bar visible for completed chats with transcript history', () => {
    const model = buildChatDetailDisplayReadModel({
      transcriptCards: [assistantCard('turn:done:assistant', 'done')],
      queuedTurns: [],
      displayedProgress: null,
      activeChat: { ...chatSummary('chat-1'), status: 'done' },
      assistantSharedFileCount: 0,
      streamState: 'idle',
      loadingActive: false,
      activeError: null,
      draft: '',
      pendingAttachmentCount: 0
    });

    expect(model.showStatusBar).toBe(true);
    expect(model.statusBar?.state).toBe('done');
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

function chatSummary(id: string): ChatSummary {
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

function progress(id: string, elapsedSeconds: number, events: SurfaceArtifact[]): ChatRunProgress {
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
