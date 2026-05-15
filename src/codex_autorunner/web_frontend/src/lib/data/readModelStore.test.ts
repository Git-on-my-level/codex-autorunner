import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatDetailPatchEvent,
  type ChatDetailSnapshot,
  type ChatIndexPatchEvent,
  type ChatIndexRow,
  type PageWindow,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { type PmaRunProgress, type SurfaceArtifact } from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/pmaChat';
import {
  PMA_LIVE_PROGRESS_EVENT_LIMIT,
  ReadModelEntityStore,
  selectChatDetailView,
  selectChatIndexView,
  selectChatIndexWindowView,
  selectorFingerprint
} from './readModelStore';

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

  it('applies chat detail timeline patches and reconciles optimistic sends', () => {
    const store = new ReadModelEntityStore();
    store.applyChatDetailSnapshot(detailSnapshot());
    expect(selectChatIndexView(store.snapshot()).rows.map((row) => row.chatId)).toEqual(['chat-1']);
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.agentProfile).toBe('m4-pma');
    expect(selectChatDetailView(store.snapshot(), 'chat-1').thread?.chatKind).toBe('coding_agent');
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

  it('does not clone untouched cached transcripts when progress updates', () => {
    const store = new ReadModelEntityStore();
    const active = pmaMessageCard('chat-1', 'turn:1:user', 'hello');
    const inactive = pmaMessageCard('chat-2', 'turn:2:user', 'cached transcript');
    store.replaceChatTranscript('chat-1', [active]);
    store.replaceChatTranscript('chat-2', [inactive]);
    const inactiveBefore = store.snapshot().chatTranscripts['chat-2'];

    store.setPmaProgress('chat-1', progress('chat-1', 1));

    expect(store.snapshot().chatTranscripts['chat-2']).toBe(inactiveBefore);
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

  it('keeps filtered chat index windows separate from cached chat entities', () => {
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

  it('returns cached chat index windows immediately by canonical request', () => {
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

    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'all', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-active']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'archived', limit: 200 }).rows.map((row) => row.chatId)).toEqual(['chat-archived']);
    expect(selectChatIndexWindowView(store.snapshot(), { filter: 'waiting', limit: 200 }).rows).toEqual([]);
  });

  it('applies entity chat-index patches once and marks affected cached windows stale', () => {
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

  it('caps retained PMA live progress events and drops hidden/debug-only progress', () => {
    const store = new ReadModelEntityStore();
    const events = [
      ...Array.from({ length: 1_000 }, (_, index) => progressArtifact(`hidden-${index}`, { kind: 'hidden', hidden: true })),
      ...Array.from({ length: PMA_LIVE_PROGRESS_EVENT_LIMIT + 5 }, (_, index) => progressArtifact(`visible-${index}`, { kind: 'tool' }))
    ];

    store.setPmaProgress('chat-1', pmaProgress(events));

    const retained = store.snapshot().pmaProgress['chat-1'].events;
    expect(retained).toHaveLength(PMA_LIVE_PROGRESS_EVENT_LIMIT);
    expect(retained.every((event) => event.id.startsWith('visible-'))).toBe(true);
    expect(retained[0].id).toBe('visible-5');
  });
});

function pmaMessageCard(chatId: string, id: string, text: string): ChatTranscriptCard {
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

function progress(chatId: string, sequence: number): PmaRunProgress {
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

function pmaProgress(events: SurfaceArtifact[]): PmaRunProgress {
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
