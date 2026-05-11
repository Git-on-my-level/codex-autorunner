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
import {
  ReadModelEntityStore,
  selectChatDetailView,
  selectChatIndexView,
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
      agent: 'codex',
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
        artifactIds: []
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
});
