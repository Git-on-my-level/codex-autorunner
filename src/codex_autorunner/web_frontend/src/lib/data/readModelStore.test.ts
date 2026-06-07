import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import type { AutomationSummary, AutomationWorkspace } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatDetailPatchEvent,
  type ChatDetailSnapshot,
  type ChatIndexPatchEvent,
  type ChatIndexRow,
  type PageWindow,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { type ChatRunProgress, type SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';
import {
  LIVE_PROGRESS_EVENT_LIMIT,
  ReadModelEntityStore,
  selectAutomationWorkspace,
  selectChatDetailView,
  selectChatIndexView,
  selectChatIndexWindowView,
  selectorFingerprint
} from './readModelStore';
import { selectChatTranscript } from './readModelViewModels';

const now = '2026-05-11T12:00:00Z';

function cursor(sequence: number): ProjectionCursor {
  return {
    value: `projection:test:${sequence}`,
    sequence,
    source: 'test',
    issuedAt: now
  };
}

function window(): PageWindow {
  return { limit: 50, totalIsExact: true, totalEstimate: 1 };
}

function chat(chatId: string, status: ChatIndexRow['status'] = 'idle'): ChatIndexRow {
  return {
    chatId,
    surface: 'pma',
    title: `Chat ${chatId}`,
    status,
    unreadCount: 0,
    lastActivityAt: now,
    repoId: 'repo-1',
    worktreeId: null,
    ticketId: null,
    runId: null,
    agent: 'codex',
    chatKind: 'pma',
    model: 'gpt-5.5',
    groupId: null
  };
}

function chatPatch(sequence: number, row: ChatIndexRow): ChatIndexPatchEvent {
  return {
    envelope: {
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      eventType: 'chat.index.patch',
      cursor: cursor(sequence),
      entityKind: 'chat',
      entityId: row.chatId,
      operation: 'patch',
      generatedAt: now
    },
    patch: {
      rows: [row],
      groups: [],
      removedRowIds: [],
      removedGroupIds: [],
      counters: { total: 1, waiting: row.status === 'waiting' ? 1 : 0, running: row.status === 'running' ? 1 : 0, unread: 0, archived: 0 }
    }
  };
}

function detailSnapshot(chatId = 'chat-1'): ChatDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'chat.detail.snapshot',
    cursor: cursor(1),
    thread: {
      chatId,
      surface: 'pma',
      title: 'Chat detail',
      status: 'running',
      repoId: 'repo-1',
      worktreeId: null,
      ticketId: null,
      runId: 'run-1',
      agent: 'hermes',
      agentProfile: 'm4-pma',
      chatKind: 'coding_agent',
      model: 'gpt-5.5',
      archived: false
    },
    timelineWindow: window(),
    timeline: [
      {
        itemId: 'item-1',
        kind: 'user_message',
        role: 'user',
        managedTurnId: 'turn-1',
        orderKey: '00000010|turn-1|user',
        section: 'user_message',
        sectionOrder: 10,
        createdAt: now,
        text: 'hello',
        artifactIds: [],
        identity: {
          timelineItemId: 'item-1',
          progressItemIds: [],
          correlationId: null
        },
        provenance: {
          sourceEventIds: ['evt-1'],
          progressEventIds: [],
          cursorEventId: null
        }
      }
    ],
    queue: { depth: 0, queuedTurnIds: [] },
    artifacts: [],
    repair: {
      snapshotRoute: `/hub/read-models/chats/${chatId}`,
      cursorQueryParam: 'after',
      gapEventType: 'projection.cursor_gap',
      behavior: 'repair_snapshot_required'
    }
  };
}

describe('read model entity store', () => {
  it('applies chat index patches idempotently by cursor', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-1')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 }
    });

    expect(store.applyChatIndexPatchEvent(chatPatch(2, chat('chat-1', 'running')))).toBe('applied');
    const versionAfterApply = store.snapshot().versions.chat['chat-1'];
    expect(store.applyChatIndexPatchEvent(chatPatch(2, chat('chat-1', 'waiting')))).toBe('ignored');

    const view = selectChatIndexView(store.snapshot());
    expect(view.rows[0].status).toBe('running');
    expect(store.snapshot().versions.chat['chat-1']).toBe(versionAfterApply);
  });

  it('marks repair required on cursor gap reset events', () => {
    const store = new ReadModelEntityStore();
    const event = chatPatch(9, chat('chat-1'));
    event.envelope.operation = 'reset';

    expect(store.applyChatIndexPatchEvent(event)).toBe('repair_required');
    expect(store.snapshot().repairRequired).toBe(true);
    expect(store.snapshot().cursors['repair.required']?.sequence).toBe(9);
  });

  it('limits selector invalidation to affected entity fingerprints', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-1'), chat('chat-2')],
      groups: [],
      counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 0 }
    });
    const chatOneBefore = selectorFingerprint(store.snapshot(), 'chat', ['chat-1']);
    const chatTwoBefore = selectorFingerprint(store.snapshot(), 'chat', ['chat-2']);

    store.applyChatIndexPatchEvent(chatPatch(2, chat('chat-2', 'waiting')));

    expect(selectorFingerprint(store.snapshot(), 'chat', ['chat-1'])).toBe(chatOneBefore);
    expect(selectorFingerprint(store.snapshot(), 'chat', ['chat-2'])).not.toBe(chatTwoBefore);
  });

  it('keys chat index windows by typed facet request and stores facet counts', () => {
    const store = new ReadModelEntityStore();
    const ticket = {
      ...chat('ticket-chat'),
      facets: {
        category: 'ticket_run' as const,
        turnKinds: ['message' as const],
        originKinds: ['surface' as const],
        transports: ['pma' as const],
        scopeKind: 'worktree' as const,
        scopeId: 'wt-1',
        agentKind: 'coding_agent' as const
      }
    };
    const request = { facets: { categories: ['ticket_run' as const], transports: ['pma' as const] }, limit: 25 };
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [ticket],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 },
      facetRequest: { categories: ['ticket_run'], turnKinds: [], originKinds: [], transports: ['pma'], scopeKinds: [], scopeIds: [], agentKinds: [] },
      facetCounts: {
        category: { ticket_run: 1 },
        turnKind: { message: 1 },
        originKind: { surface: 1 },
        transport: { pma: 1 },
        scopeKind: { worktree: 1 },
        agentKind: { coding_agent: 1 }
      }
    }, request);

    const typedWindow = selectChatIndexWindowView(store.snapshot(), request);
    expect(typedWindow.rows.map((row) => row.chatId)).toEqual(['ticket-chat']);
    expect(typedWindow.facetCounts.category.ticket_run).toBe(1);
    expect(selectChatIndexWindowView(store.snapshot(), { facets: { categories: ['automation'] }, limit: 25 }).rows).toEqual([]);
  });

  it('preserves hydrated automation detail jobs without hiding fresh index fields', () => {
    const store = new ReadModelEntityStore();
    store.applyAutomationWorkspaceSnapshot(automationWorkspace([
      automationSummary({
        id: 'rule-1',
        name: 'Cached detail',
        enabled: false,
        updatedAt: '2026-05-11T12:00:00Z',
        jobs: [automationJob('old-1'), automationJob('old-2')],
        jobCount: 3,
        lastJob: automationJob('old-2', 'succeeded'),
        raw: { executor: { prompt: 'full detail prompt' } }
      })
    ]));

    store.applyAutomationWorkspaceSnapshot(automationWorkspace([
      automationSummary({
        id: 'rule-1',
        name: 'Fresh index',
        enabled: true,
        updatedAt: '2026-05-11T12:05:00Z',
        jobs: [],
        jobCount: 3,
        lastJob: automationJob('fresh-1', 'failed'),
        raw: { shallow: true }
      })
    ]));

    const row = selectAutomationWorkspace(store.snapshot())?.automations[0];
    expect(row).toMatchObject({
      name: 'Fresh index',
      enabled: true,
      updatedAt: '2026-05-11T12:05:00Z',
      lastJob: { jobId: 'fresh-1', effectiveState: 'failed' },
      jobCount: 3,
      raw: { executor: { prompt: 'full detail prompt' } }
    });
    expect(row?.jobs.map((job) => job.jobId)).toEqual(['old-1', 'old-2']);
  });

  it('applies chat detail timeline patches and reconciles optimistic sends', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot());
    expect(selectChatIndexView(store.snapshot()).rows.map((row) => row.chatId)).toEqual(['chat-1']);
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.agentProfile).toBe('m4-pma');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.chatKind).toBe('coding_agent');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline[0]).toMatchObject({
      managedTurnId: 'turn-1',
      orderKey: '00000010|turn-1|user',
      section: 'user_message',
      sectionOrder: 10
    });
    store.optimisticSend(
      'chat-1',
      {
        itemId: 'optimistic:client-1',
        kind: 'user_message',
        role: 'user',
        createdAt: now,
        text: 'optimistic',
        artifactIds: [],
        clientMessageId: 'client-1'
      },
      'client-1'
    );
    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline.map((item) => item.itemId)).toContain('optimistic:client-1');

    store.reconcileOptimisticTimelineItem(
      'chat-1',
      'client-1',
      {
        itemId: 'item-2',
        kind: 'user_message',
        role: 'user',
        createdAt: now,
        text: 'optimistic',
        artifactIds: [],
        clientMessageId: 'client-1',
        backendMessageId: 'turn-2'
      }
    );

    const timelineIds = selectChatDetailView(store.snapshot(), 'chat-1').timeline.map((item) => item.itemId);
    expect(timelineIds).not.toContain('optimistic:client-1');
    expect(timelineIds).toContain('item-2');
    expect(store.snapshot().optimistic['client-1'].status).toBe('reconciled');
  });

  it('keeps same-turn activity after the user row when backend order keys arrive out of phase order', () => {
    const store = new ReadModelEntityStore();
    const snapshot = detailSnapshot();
    snapshot.timeline = [
      {
        itemId: 'turn-1-progress',
        kind: 'progress',
        managedTurnId: 'turn-1',
        orderKey: '00000005|turn-1|progress',
        section: 'activity',
        sectionOrder: 20,
        createdAt: now,
        text: 'Starting',
        artifactIds: []
      },
      {
        itemId: 'turn-1-user',
        kind: 'user_message',
        role: 'user',
        managedTurnId: 'turn-1',
        orderKey: '00000010|turn-1|user',
        section: 'user_message',
        sectionOrder: 10,
        createdAt: now,
        text: 'hello',
        artifactIds: []
      },
      {
        itemId: 'turn-1-assistant',
        kind: 'assistant_message',
        role: 'assistant',
        managedTurnId: 'turn-1',
        orderKey: '00000015|turn-1|assistant',
        section: 'assistant_message',
        sectionOrder: 30,
        createdAt: now,
        text: 'hi',
        artifactIds: []
      }
    ];

    store.applyChatDetailSnapshot(snapshot);

    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline.map((item) => item.itemId)).toEqual([
      'turn-1-user',
      'turn-1-progress',
      'turn-1-assistant'
    ]);
  });

  it('replaces and upserts backend-owned PMA transcript cards independently of timeline state', () => {
    const store = new ReadModelEntityStore();
    const first: ChatTranscriptCard = {
      kind: 'message',
      id: 'turn:1:user',
      turnId: '1',
      orderKey: '001',
      timestamp: now,
      message: {
        id: 'turn:1:user',
        chatId: 'chat-1',
        role: 'user',
        text: 'hello',
        createdAt: now,
        status: null,
        artifacts: [],
        raw: {}
      }
    };
    const second: ChatTranscriptCard = {
      ...first,
      id: 'turn:1:assistant',
      orderKey: '002',
      message: {
        ...first.message,
        id: 'turn:1:assistant',
        role: 'assistant',
        text: 'hi'
      }
    };

    store.replaceChatTranscript('chat-1', [first]);
    store.upsertChatTranscriptCards('chat-1', [second]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual(['turn:1:user', 'turn:1:assistant']);
    expect(store.snapshot().chatTranscripts['chat-1'].cardsById['turn:1:assistant']).toMatchObject({
      kind: 'message',
      message: { role: 'assistant', text: 'hi' }
    });
  });

  it('reconciles optimistic user transcript rows when matching backend rows append later', () => {
    const store = new ReadModelEntityStore();
    const optimistic = pmaMessageCard('chat-1', 'optimistic:user:client-1', 'queued prompt');
    const unrelatedOptimistic = pmaMessageCard('chat-1', 'optimistic:user:client-2', 'still pending');
    const backendUser = pmaMessageCard('chat-1', 'turn:1:user', 'queued prompt');
    optimistic.message.raw = {
      optimistic: true,
      client_turn_id: 'client-1',
      correlation_id: 'client-1'
    };
    unrelatedOptimistic.message.raw = {
      optimistic: true,
      client_turn_id: 'client-2',
      correlation_id: 'client-2'
    };
    backendUser.message.raw = {
      identity: { correlation_id: 'client-1' }
    };

    store.upsertChatTranscriptCards('chat-1', [optimistic, unrelatedOptimistic]);
    store.upsertChatTranscriptCards('chat-1', [backendUser]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual([
      'optimistic:user:client-2',
      'turn:1:user'
    ]);
  });

  it('does not clone untouched cached transcripts when progress updates', () => {
    const store = new ReadModelEntityStore();
    const active = pmaMessageCard('chat-1', 'turn:1:user', 'hello');
    const inactive = pmaMessageCard('chat-2', 'turn:2:user', 'cached transcript');
    store.replaceChatTranscript('chat-1', [active]);
    store.replaceChatTranscript('chat-2', [inactive]);
    const inactiveBefore = store.snapshot().chatTranscripts['chat-2'];

    store.setChatProgress('chat-1', progress('chat-1', 1));

    expect(store.snapshot().chatTranscripts['chat-2']).toBe(inactiveBefore);
  });

  it('keeps selected transcript arrays stable across unrelated state updates', () => {
    const store = new ReadModelEntityStore();
    store.replaceChatTranscript('chat-1', [pmaMessageCard('chat-1', 'turn:1:user', 'hello')]);
    const selectedBefore = selectChatTranscript(store.snapshot(), 'chat-1');

    store.setChatProgress('chat-1', progress('chat-1', 1));

    expect(selectChatTranscript(store.snapshot(), 'chat-1')).toBe(selectedBefore);
  });

  it('selects backend-owned transcript rows without semantic compaction', () => {
    const store = new ReadModelEntityStore();
    store.replaceChatTranscript('chat-1', [
      pmaMessageCard('chat-1', 'turn:1:user', 'Investigate'),
      pmaTraceCard('thinking-1', 'Need', ['evt-1']),
      pmaTraceCard('thinking-2', 'to inspect', ['evt-2'])
    ]);

    const selectedBefore = selectChatTranscript(store.snapshot(), 'chat-1');

    expect(selectedBefore.map((card) => card.id)).toEqual([
      'thinking-1',
      'thinking-2',
      'turn:1:user'
    ]);

    store.setChatProgress('chat-1', progress('chat-1', 1));

    expect(selectChatTranscript(store.snapshot(), 'chat-1')).toBe(selectedBefore);
  });

  it('keeps append-heavy transcript selection memoized without compacting rows', () => {
    const store = new ReadModelEntityStore();
    const baseCards = Array.from({ length: 1_000 }, (_, index) =>
      pmaTraceCard(`thinking-${index}`, `token-${index}`, [`evt-${index}`], index)
    );
    store.replaceChatTranscript('chat-1', baseCards);
    const selectedBefore = selectChatTranscript(store.snapshot(), 'chat-1');

    expect(selectedBefore).toHaveLength(1_000);
    expect(selectedBefore[0]).toMatchObject({ kind: 'intermediate', id: 'thinking-0' });
    expect(selectChatTranscript(store.snapshot(), 'chat-1')).toBe(selectedBefore);

    store.upsertChatTranscriptCards('chat-1', [
      pmaTraceCard('thinking-1000', 'tail-token', ['evt-1000'], 1000)
    ]);
    const selectedAfter = selectChatTranscript(store.snapshot(), 'chat-1');

    expect(selectedAfter).not.toBe(selectedBefore);
    expect(selectedAfter).toHaveLength(1_001);
    expect(selectedAfter.at(-1)).toMatchObject({ kind: 'intermediate', id: 'thinking-1000' });
  });

  it('does not clone untouched cached details when another detail patches', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot('chat-1'));
    store.applyChatDetailSnapshot(detailSnapshot('chat-2'));
    const detailBefore = store.snapshot().chatDetails['chat-2'];
    const timelineBefore = store.snapshot().timelines['chat-2'];

    store.applyChatDetailPatchEvent({
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.detail.patch',
        cursor: cursor(2),
        entityKind: 'chat',
        entityId: 'chat-1',
        operation: 'patch',
        generatedAt: now
      },
      patch: {
        thread: null,
        appendedTimeline: [
          {
            itemId: 'item-2',
            kind: 'assistant_message',
            role: 'assistant',
            createdAt: now,
            text: 'hi',
            artifactIds: []
          }
        ],
        patchedTimeline: [],
        removedTimelineIds: [],
        queue: null,
        artifacts: []
      }
    });

    expect(store.snapshot().chatDetails['chat-2']).toBe(detailBefore);
    expect(store.snapshot().timelines['chat-2']).toBe(timelineBefore);
  });

  it('orders backend-owned PMA transcript cards by backend order key across turns', () => {
    const store = new ReadModelEntityStore();
    const userOne: ChatTranscriptCard = {
      kind: 'message',
      id: 'turn:1:user',
      turnId: '1',
      orderKey: '00000001|user',
      timestamp: now,
      message: {
        id: 'turn:1:user',
        chatId: 'chat-1',
        role: 'user',
        text: 'first',
        createdAt: now,
        status: null,
        artifacts: [],
        raw: {}
      }
    };
    const assistantOne: ChatTranscriptCard = {
      ...userOne,
      id: 'turn:1:assistant',
      orderKey: '00000002|assistant',
      message: { ...userOne.message, id: 'turn:1:assistant', role: 'assistant', text: 'first reply' }
    };
    const userTwo: ChatTranscriptCard = {
      ...userOne,
      id: 'turn:2:user',
      turnId: '2',
      orderKey: '00000003|user',
      message: { ...userOne.message, id: 'turn:2:user', text: 'second' }
    };

    store.replaceChatTranscript('chat-1', [userTwo, assistantOne, userOne]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual([
      'turn:1:user',
      'turn:1:assistant',
      'turn:2:user'
    ]);
  });

  it('keeps an optimistic user transcript row before live progress that arrives first', () => {
    const store = new ReadModelEntityStore();
    const optimistic: ChatTranscriptCard = {
      kind: 'message',
      id: 'optimistic:user:1',
      turnId: null,
      orderKey: 'optimistic|2026-05-11T12:00:00.000Z|optimistic:user:1',
      timestamp: '2026-05-11T12:00:00.000Z',
      message: {
        id: 'optimistic:user:1',
        chatId: 'chat-1',
        role: 'user',
        text: 'What tools do you have access to?',
        createdAt: '2026-05-11T12:00:00.000Z',
        status: null,
        artifacts: [],
        raw: { optimistic: true }
      }
    };
    const progress: ChatTranscriptCard = {
      kind: 'intermediate',
      id: 'turn:1:intermediate:0001',
      title: 'Chat Execution Journal',
      text: 'Execution started.',
      eventIds: ['1'],
      progressSourceIds: ['1'],
      detail: null,
      turnId: '1',
      orderKey: '00000001|2026-05-11T12:00:01.000Z|turn:1:intermediate:0001',
      timestamp: '2026-05-11T12:00:01.000Z'
    };
    const canonicalUser: ChatTranscriptCard = {
      ...optimistic,
      id: 'turn:1:user',
      turnId: '1',
      orderKey: '00000000|2026-05-11T12:00:00.000Z|turn:1:user',
      message: {
        ...optimistic.message,
        id: 'turn:1:user',
        raw: {}
      }
    };

    store.upsertChatTranscriptCards('chat-1', [progress]);
    store.upsertChatTranscriptCards('chat-1', [optimistic]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual(['optimistic:user:1', 'turn:1:intermediate:0001']);

    store.replaceChatTranscript('chat-1', [progress, canonicalUser]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual(['turn:1:user', 'turn:1:intermediate:0001']);
  });

  it('keeps live progress below the same-turn user when event sequence ties', () => {
    const store = new ReadModelEntityStore();
    const user: ChatTranscriptCard = {
      kind: 'message',
      id: 'turn:1:user',
      turnId: '1',
      orderKey: '00000001|2026-05-11T12:00:00.000Z|turn:1:user',
      timestamp: '2026-05-11T12:00:00.000Z',
      message: {
        id: 'turn:1:user',
        chatId: 'chat-1',
        role: 'user',
        text: 'What model are you?',
        createdAt: '2026-05-11T12:00:00.000Z',
        status: null,
        artifacts: [],
        raw: {}
      }
    };
    const progress: ChatTranscriptCard = {
      kind: 'intermediate',
      id: 'turn:1:intermediate:0001',
      title: 'Chat Execution Journal',
      text: 'Execution started.',
      eventIds: ['1'],
      progressSourceIds: ['1'],
      detail: null,
      turnId: '1',
      orderKey: '00000001|2026-05-11T12:00:00.000Z|progress:notice:0001',
      timestamp: '2026-05-11T12:00:00.000Z'
    };

    store.replaceChatTranscript('chat-1', [progress, user]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual(['turn:1:user', 'turn:1:intermediate:0001']);
  });

  it('orders later live progress after an earlier assistant despite low live event sequence', () => {
    const store = new ReadModelEntityStore();
    const firstUser: ChatTranscriptCard = {
      kind: 'message',
      id: 'turn:1:user',
      turnId: '1',
      orderKey: '00000001|2026-05-11T12:00:00.000Z|turn:1:user',
      timestamp: '2026-05-11T12:00:00.000Z',
      message: {
        id: 'turn:1:user',
        chatId: 'chat-1',
        role: 'user',
        text: 'What model are you?',
        createdAt: '2026-05-11T12:00:00.000Z',
        status: null,
        artifacts: [],
        raw: {}
      }
    };
    const firstAssistant: ChatTranscriptCard = {
      ...firstUser,
      id: 'turn:1:assistant',
      orderKey: '00000003|2026-05-11T12:00:08.000Z|turn:1:assistant',
      timestamp: '2026-05-11T12:00:08.000Z',
      message: {
        ...firstUser.message,
        id: 'turn:1:assistant',
        role: 'assistant',
        text: 'I am Codex.',
        createdAt: '2026-05-11T12:00:08.000Z'
      }
    };
    const secondProgress: ChatTranscriptCard = {
      kind: 'intermediate',
      id: 'turn:2:intermediate:0001',
      title: 'Chat Execution Journal',
      text: 'Execution started.',
      eventIds: ['1'],
      progressSourceIds: ['1'],
      detail: null,
      turnId: '2',
      orderKey: '00000001|2026-05-11T12:00:27.000Z|progress:notice:0001',
      timestamp: '2026-05-11T12:00:27.000Z'
    };

    store.replaceChatTranscript('chat-1', [secondProgress, firstAssistant, firstUser]);

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual([
      'turn:1:user',
      'turn:1:assistant',
      'turn:2:intermediate:0001'
    ]);
  });

  it('keeps detail-loaded chats addressable when a bounded index snapshot arrives later', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot('deep-linked-chat'));

    store.applyChatIndexSnapshot({
      cursor: cursor(2),
      rows: [chat('chat-1')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 }
    });

    const view = selectChatIndexView(store.snapshot());
    expect(view.rows.map((row) => row.chatId)).toEqual(['chat-1', 'deep-linked-chat']);
    expect(view.rows[1].title).toBe('Chat detail');
    expect(selectChatDetailView(store.snapshot(), 'deep-linked-chat').thread?.title).toBe('Chat detail');
  });

  it('deduplicates full chat index snapshots by durable chat id', () => {
    const store = new ReadModelEntityStore();
    const first = chat('chat-1', 'waiting');
    first.title = 'Original title';
    const latest = chat('chat-1', 'running');
    latest.title = 'Latest title';

    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [first, chat('chat-2'), latest],
      groups: [],
      counters: { total: 3, waiting: 1, running: 1, unread: 0, archived: 0 }
    });

    const view = selectChatIndexView(store.snapshot());
    expect(view.rows.map((row) => row.chatId)).toEqual(['chat-1', 'chat-2']);
    expect(view.rows[0].title).toBe('Latest title');
    expect(view.rows[0].status).toBe('running');
  });

  it('preserves bounded chat index windows when filters change', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-active', 'running'), chat('chat-waiting', 'waiting')],
      groups: [],
      counters: { total: 2, waiting: 1, running: 1, unread: 0, archived: 1 },
      filter: 'all',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 2 }
    }, { filter: 'all', limit: 200 });

    store.applyChatIndexSnapshot({
      cursor: cursor(2),
      rows: [chat('chat-archived', 'archived')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 1 },
      filter: 'archived',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'archived', limit: 200 });

    expect(Object.keys(store.snapshot().chats).sort()).toEqual(['chat-active', 'chat-archived', 'chat-waiting']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 200 }).rows.map((row) => row.chatId)).toEqual([
      'chat-active',
      'chat-waiting'
    ]);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'archived', limit: 200 }).rows.map((row) => row.chatId)).toEqual([
      'chat-archived'
    ]);
    expect(store.snapshot().chatCounters).toEqual({ total: 2, waiting: 1, running: 1, unread: 0, archived: 1 });
  });

  it('keeps previously loaded filter windows addressable after a default refresh', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-active', 'running')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 1, unread: 0, archived: 0 },
      filter: 'all',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'all', limit: 200 });
    store.applyChatIndexSnapshot({
      cursor: cursor(2),
      rows: [chat('chat-archived', 'archived')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 1 },
      filter: 'archived',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'archived', limit: 200 });

    store.applyChatIndexSnapshot({
      cursor: cursor(3),
      rows: [chat('chat-visible')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 1 },
      filter: 'all',
      query: null,
      window: { limit: 50, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'all', limit: 50 });

    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-active']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'archived', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-archived']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'waiting', limit: 200 }).rows).toEqual([]);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 50 }).rows.map((row) => row.chatId)).toEqual(['chat-visible']);
    expect(Object.keys(store.snapshot().chats).sort()).toEqual(['chat-active', 'chat-archived', 'chat-visible']);
  });

  it('applies entity chat-index patches once and marks the active cached window stale', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-active', 'running'), chat('chat-waiting', 'waiting')],
      groups: [],
      counters: { total: 2, waiting: 1, running: 1, unread: 0, archived: 0 },
      filter: 'all',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 2 }
    }, { filter: 'all', limit: 200 });
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-active', 'running')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 1, unread: 0, archived: 0 },
      filter: 'active',
      query: null,
      window: { limit: 200, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'active', limit: 200 });

    const archived = chat('chat-active', 'archived');
    expect(store.applyChatIndexPatchEvent({
      ...chatPatch(2, archived),
      patch: {
        ...chatPatch(2, archived).patch,
        order: ['chat-active', 'chat-waiting'],
        counters: { total: 2, waiting: 1, running: 0, unread: 0, archived: 1 }
      }
    })).toBe('applied');

    const activeWindow = selectChatIndexWindowView(store.snapshot(), { filter: 'active', limit: 200 });
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 200 }).rows.map((row) => row.chatId)).toEqual([
      'chat-active',
      'chat-waiting'
    ]);
    expect(activeWindow.rows.map((row) => row.chatId)).toEqual([]);
    expect(activeWindow.window?.status).toBe('interrupted');
    expect(activeWindow.window?.refreshing).toBe(true);
    expect(store.snapshot().cursors['chat.index'].sequence).toBe(2);
  });

  it('does not retain off-window archived row patches in the chat index cache', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(10),
      rows: [chat('chat-visible')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 3000 },
      filter: 'all',
      query: null,
      window: { limit: 1, totalIsExact: true, totalEstimate: 1 }
    }, { filter: 'all', limit: 1 });

    expect(store.applyChatIndexPatchEvent({
      ...chatPatch(11, chat('chat-archived-history', 'archived')),
      patch: {
        rows: [chat('chat-archived-history', 'archived')],
        groups: [],
        removedRowIds: [],
        removedGroupIds: [],
        counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 3001 }
      }
    })).toBe('applied');

    expect(Object.keys(store.snapshot().chats)).toEqual(['chat-visible']);
    expect(store.snapshot().chatCounters.archived).toBe(3001);
  });

  it('marks the default chat window interrupted when archive patches under-fill it', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-visible'), chat('chat-next-page')],
      groups: [],
      counters: { total: 3, waiting: 0, running: 0, unread: 0, archived: 0 },
      filter: 'all',
      query: null,
      window: { limit: 2, totalIsExact: true, totalEstimate: 3 }
    }, { filter: 'all', limit: 2 });

    expect(store.applyChatIndexPatchEvent({
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.index.patch',
        cursor: cursor(2),
        entityKind: 'chat',
        entityId: 'chat-visible',
        operation: 'patch',
        generatedAt: now
      },
      patch: {
        rows: [],
        groups: [],
        removedRowIds: ['chat-visible'],
        removedGroupIds: [],
        counters: { total: 2, waiting: 0, running: 0, unread: 0, archived: 1 }
      }
    })).toBe('applied');

    const allWindow = selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 2 });
    expect(allWindow.rows.map((row) => row.chatId)).toEqual(['chat-next-page']);
    expect(allWindow.window?.status).toBe('interrupted');
    expect(allWindow.window?.refreshing).toBe(true);
  });

  it('preserves appended default chat pages when live order patches arrive', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-1'), chat('chat-2')],
      groups: [],
      counters: { total: 4, waiting: 0, running: 0, unread: 0, archived: 0 },
      filter: 'all',
      query: null,
      window: { limit: 2, nextCursor: '2', totalIsExact: true, totalEstimate: 4 }
    }, { filter: 'all', limit: 2 });
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-3'), chat('chat-4')],
      groups: [],
      counters: { total: 4, waiting: 0, running: 0, unread: 0, archived: 0 },
      filter: 'all',
      query: null,
      window: { limit: 2, nextCursor: null, previousCursor: '0', totalIsExact: true, totalEstimate: 4 }
    }, { filter: 'all', limit: 2 }, { append: true });

    expect(store.applyChatIndexPatchEvent({
      ...chatPatch(2, chat('chat-2', 'running')),
      patch: {
        ...chatPatch(2, chat('chat-2', 'running')).patch,
        order: ['chat-2', 'chat-1'],
        counters: { total: 4, waiting: 0, running: 1, unread: 0, archived: 0 }
      }
    })).toBe('applied');

    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 2 }).rows.map((row) => row.chatId)).toEqual([
      'chat-2',
      'chat-1',
      'chat-3',
      'chat-4'
    ]);
  });

  it('preserves chat kind when detail snapshots omit the durable field', () => {
    const store = new ReadModelEntityStore();
    const row = chat('chat-1');
    row.chatKind = 'coding_agent';
    row.title = 'Renamed thread';
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [row],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 }
    });
    const snapshot = detailSnapshot('chat-1');
    delete snapshot.thread.chatKind;

    store.applyChatDetailSnapshot(snapshot);

    expect(selectChatIndexView(store.snapshot()).rows[0].chatKind).toBe('coding_agent');
  });

  it('rolls back failed optimistic sends', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot());
    store.optimisticSend(
      'chat-1',
      {
        itemId: 'optimistic:client-2',
        kind: 'user_message',
        role: 'user',
        createdAt: now,
        text: 'will fail',
        artifactIds: [],
        clientMessageId: 'client-2'
      },
      'client-2'
    );

    store.failOptimisticMutation('client-2');

    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline.map((item) => item.itemId)).not.toContain('optimistic:client-2');
    expect(store.snapshot().optimistic['client-2'].status).toBe('failed');
  });

  it('tracks optimistic read markers and can revert them', () => {
    const store = new ReadModelEntityStore();
    store.setReadMarkers({ 'chat-1': now });
    store.optimisticReadMarkers({ 'chat-1': now, 'chat-2': now }, 'read-1');

    expect(store.snapshot().readMarkers['chat-2']).toBe(now);
    expect(store.snapshot().optimistic['read-1'].status).toBe('pending');

    store.revertOptimisticMutation('read-1');

    expect(store.snapshot().readMarkers['chat-1']).toBe(now);
    expect(store.snapshot().readMarkers['chat-2']).toBeUndefined();
    expect(store.snapshot().optimistic['read-1'].status).toBe('reverted');
  });

  it('is a Svelte-readable store', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor: cursor(1),
      rows: [chat('chat-1')],
      groups: [],
      counters: { total: 1, waiting: 0, running: 0, unread: 0, archived: 0 }
    });

    expect(get(store).chats['chat-1'].title).toBe('Chat chat-1');
  });

  it('applies chat detail patches once', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot());
    const event: ChatDetailPatchEvent = {
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.detail.patch',
        cursor: cursor(2),
        entityKind: 'chat',
        entityId: 'chat-1',
        operation: 'upsert',
        generatedAt: now
      },
      patch: {
        appendedTimeline: [
          {
            itemId: 'item-2',
            kind: 'assistant_message',
            role: 'assistant',
            managedTurnId: 'turn-1',
            orderKey: '00000030|turn-1|assistant',
            section: 'assistant_message',
            sectionOrder: 30,
            createdAt: now,
            text: 'done',
            artifactIds: []
          }
        ],
        patchedTimeline: [],
        removedTimelineIds: [],
        queue: { depth: 0, queuedTurnIds: [] },
        artifacts: []
      }
    };

    expect(store.applyChatDetailPatchEvent(event)).toBe('applied');
    expect(store.applyChatDetailPatchEvent(event)).toBe('ignored');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline.filter((item) => item.itemId === 'item-2')).toHaveLength(1);
  });

  it('keeps same-turn patched activity after the user row when its order key is earlier', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot());
    const event: ChatDetailPatchEvent = {
      envelope: {
        contractVersion: READ_MODEL_CONTRACT_VERSION,
        eventType: 'chat.detail.patch',
        cursor: cursor(2),
        entityKind: 'chat',
        entityId: 'chat-1',
        operation: 'upsert',
        generatedAt: now
      },
      patch: {
        appendedTimeline: [
          {
            itemId: 'turn-1-progress',
            kind: 'progress',
            managedTurnId: 'turn-1',
            orderKey: '00000005|turn-1|progress',
            section: 'activity',
            sectionOrder: 20,
            createdAt: now,
            text: 'Starting',
            artifactIds: []
          }
        ],
        patchedTimeline: [],
        removedTimelineIds: [],
        queue: null,
        artifacts: []
      }
    };

    expect(store.applyChatDetailPatchEvent(event)).toBe('applied');

    expect(selectChatDetailView(store.snapshot(), 'chat-1').timeline.map((item) => item.itemId)).toEqual([
      'item-1',
      'turn-1-progress'
    ]);
  });

  it('caps retained PMA live progress events and drops hidden/debug-only progress', () => {
    const store = new ReadModelEntityStore();
    const events = [
      ...Array.from({ length: 1_000 }, (_, index) => progressArtifact(`hidden-${index}`, { kind: 'hidden', hidden: true })),
      ...Array.from({ length: LIVE_PROGRESS_EVENT_LIMIT + 5 }, (_, index) => progressArtifact(`visible-${index}`, { kind: 'tool' }))
    ];

    store.setChatProgress('chat-1', chatProgress(events));

    const retained = store.snapshot().chatProgress['chat-1'].events;
    expect(retained).toHaveLength(LIVE_PROGRESS_EVENT_LIMIT);
    expect(retained.every((event) => event.id.startsWith('visible-'))).toBe(true);
    expect(retained[0].id).toBe('visible-5');
  });
});

function pmaMessageCard(chatId: string, id: string, text: string): Extract<ChatTranscriptCard, { kind: 'message' }> {
  return {
    kind: 'message',
    id,
    turnId: id,
    orderKey: id,
    timestamp: now,
    message: {
      id,
      chatId,
      role: 'user',
      text,
      createdAt: now,
      status: null,
      artifacts: [],
      raw: {}
    }
  };
}

function pmaTraceCard(id: string, text: string, eventIds: string[], index = 1): ChatTranscriptCard {
  const order = String(index).padStart(6, '0');
  return {
    kind: 'intermediate',
    id,
    title: 'Thinking',
    text,
    eventIds,
    progressSourceIds: [],
    detail: null,
    turnId: 'turn-1',
    orderKey: `${order}|turn-1|activity|${id}`,
    timestamp: now
  };
}

function progress(chatId: string, sequence: number): ChatRunProgress {
  return {
    id: `turn-${sequence}`,
    chatId,
    status: 'running',
    workStatus: 'running',
    operatorStatus: null,
    streamShouldClose: false,
    streamCloseReason: null,
    terminal: false,
    phase: null,
    guidance: null,
    queueDepth: 0,
    elapsedSeconds: sequence,
    startedAt: now,
    idleSeconds: null,
    lastEventId: sequence,
    lastEventAt: now,
    progressPercent: null,
    events: [],
    raw: {}
  };
}

function progressArtifact(id: string, progressItem: Record<string, unknown>): SurfaceArtifact {
  return {
    id,
    kind: 'progress',
    title: id,
    summary: null,
    url: null,
    createdAt: now,
    raw: { progress_item: { item_id: id, title: id, ...progressItem } }
  };
}

function automationWorkspace(automations: AutomationSummary[]): AutomationWorkspace {
  return {
    automations,
    presets: [],
    summary: {
      total: automations.length,
      active: automations.filter((automation) => automation.enabled).length,
      paused: automations.filter((automation) => !automation.enabled).length,
      failedJobs: automations.filter((automation) => automation.lastJob?.effectiveState === 'failed').length
    },
    targetOptions: [],
    agentDefaults: {
      defaultAgent: 'codex',
      defaultProfile: null,
      defaultModel: null,
      defaultReasoning: null,
      raw: {}
    },
    generatedAt: now
  };
}

function automationSummary(overrides: Partial<AutomationSummary> = {}): AutomationSummary {
  return {
    id: 'rule-1',
    name: 'Automation',
    enabled: true,
    systemOwned: false,
    kind: 'scheduled',
    executorKind: 'agent_task_turn',
    targetPolicy: 'repo_required',
    target: {},
    metadata: {},
    schedule: null,
    lastJob: null,
    jobs: [],
    jobCount: 0,
    createdAt: now,
    updatedAt: now,
    product: {
      productApiVersion: 1,
      editable: {
        canEnable: true,
        canRename: true,
        canEditSchedule: true,
        canEditMessage: true,
        canEditTicketBody: true,
        canRunNow: true,
        canEditRaw: true,
        rawEditBlockedReason: '',
        managedReason: null,
        raw: {}
      },
      managed: {
        systemOwned: false,
        managed: false,
        reason: null,
        raw: {}
      },
      scheduleEditor: {
        kind: 'daily',
        editable: true,
        summary: 'Daily',
        timezone: 'UTC',
        nextFireAt: null,
        lastFireAt: null,
        state: 'active',
        fields: {},
        raw: {}
      },
      triggerSummary: {
        kind: 'schedule',
        label: 'Schedule',
        eventTypes: ['schedule.fire'],
        filters: {},
        raw: {}
      },
      targetSummary: {},
      message: {
        source: 'executor',
        field: 'prompt',
        preview: '',
        template: false,
        editable: true,
        raw: {}
      },
      messageSource: 'prompt',
      messagePreview: '',
      actionPreview: {},
      executorSummary: {},
      policySummary: {},
      diagnostics: [],
      rawLinks: {}
    },
    raw: {},
    ...overrides
  };
}

function automationJob(id: string, effectiveState = 'succeeded') {
  return {
    jobId: id,
    state: effectiveState,
    rawState: effectiveState,
    effectiveState,
    createdAt: now,
    startedAt: now,
    finishedAt: now,
    updatedAt: now,
    resultSummary: null,
    errorText: null,
    attemptCount: 1,
    blockedByJobId: null,
    blockedReason: null,
    blockedAt: null,
    pmaQueueResult: null,
    childExecution: null,
    children: [],
    runtimeContract: null,
    terminalReason: null,
    policyViolations: [],
    raw: {}
  };
}

function chatProgress(events: SurfaceArtifact[]): ChatRunProgress {
  return {
    id: 'run-1',
    chatId: 'chat-1',
    status: 'running',
    workStatus: 'running',
    operatorStatus: 'running',
    terminal: false,
    streamShouldClose: false,
    streamCloseReason: null,
    phase: 'running',
    guidance: null,
    queueDepth: 0,
    elapsedSeconds: 1,
    startedAt: now,
    idleSeconds: null,
    lastEventId: 1,
    lastEventAt: now,
    progressPercent: null,
    events,
    raw: {}
  };
}
