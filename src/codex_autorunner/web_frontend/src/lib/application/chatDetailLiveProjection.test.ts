import { describe, expect, it } from 'vitest';
import type { ApiError, ApiResult, PmaThreadQueue } from '$lib/api/client';
import type { TranscriptStreamOptions } from '$lib/api/streaming';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import {
  ChatDetailLiveProjection,
  type ChatDetailLiveProjectionApi,
  type ChatDetailLiveProjectionDeps
} from '$lib/application/chatDetailLiveProjection';
import { buildOptimisticUserTranscriptCard } from '$lib/application/pmaChatArchitecture';
import type { PmaChatSummary } from '$lib/viewModels/domain';
import type { ChatTranscriptCard, ChatTranscriptSnapshot } from '$lib/viewModels/pmaChat';

const now = '2026-05-18T01:02:03.000Z';

describe('ChatDetailLiveProjection', () => {
  it('loads transcript and queue snapshots while preserving pending optimistic user cards', async () => {
    const store = new ReadModelEntityStore();
    const optimistic = buildOptimisticUserTranscriptCard('chat-1', 'pending user text', 'optimistic:user:client-1', now);
    store.upsertChatTranscriptCards('chat-1', [optimistic]);
    const api = apiFixture({
      transcriptRows: [messageCard('chat-1', 'assistant-1', 'assistant', 'hello')],
      queuedTurns: [queuedTurn('turn-2')]
    });
    const projection = projectionFixture(store, api, { shouldUseStream: () => false });

    await projection.refresh('chat-1');

    const snapshot = store.snapshot();
    expect(snapshot.chatTranscripts['chat-1'].order).toEqual(['assistant-1', 'optimistic:user:client-1']);
    expect(snapshot.pmaQueues['chat-1'].map((turn) => turn.managedTurnId)).toEqual(['turn-2']);
    expect(projection.snapshot().loadingActive).toBe(false);
  });

  it('applies transcript append and patch events to normalized read-model state', () => {
    const store = new ReadModelEntityStore();
    const stream = streamFixture();
    const projection = projectionFixture(store, apiFixture(), { openStream: stream.open });

    projection.connect('chat-1');
    stream.options?.onEvent({
      kind: 'transcript_append',
      lastEventId: '1',
      payload: { rows: [rawMessageRow('chat-1', 'assistant-1', 'assistant', 'hello', 'turn-1')] }
    });
    stream.options?.onEvent({
      kind: 'transcript_patch',
      lastEventId: '2',
      payload: { status: rawProgress('chat-1', 'turn-1', { elapsed_seconds: 7 }) }
    });

    const snapshot = store.snapshot();
    expect(snapshot.chatTranscripts['chat-1'].order).toEqual(['assistant-1']);
    expect(snapshot.pmaProgress['chat-1'].id).toBe('turn-1');
    expect(snapshot.pmaProgress['chat-1'].elapsedSeconds).toBe(7);
  });

  it('schedules one bounded repair snapshot after interrupted streams', async () => {
    const store = new ReadModelEntityStore();
    const timers = timerFixture();
    const stream = streamFixture();
    const api = apiFixture({ transcriptRows: [messageCard('chat-1', 'assistant-1', 'assistant', 'repaired')] });
    const projection = projectionFixture(store, api, {
      openStream: stream.open,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    });

    projection.connect('chat-1');
    stream.options?.onError?.(new Event('error'));
    stream.options?.onError?.(new Event('error'));

    expect(timers.pending()).toHaveLength(1);
    timers.runPending();
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getTranscriptCalls).toBe(1);
    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual(['assistant-1']);
    expect(projection.snapshot().streamState).toBe('interrupted');
  });

  it('cancels pending repair refreshes when the stream reconnects before the repair fires', async () => {
    const store = new ReadModelEntityStore();
    const timers = timerFixture();
    const stream = streamFixture();
    const api = apiFixture({ transcriptRows: [messageCard('chat-1', 'assistant-1', 'assistant', 'repaired')] });
    const projection = projectionFixture(store, api, {
      openStream: stream.open,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    });

    projection.connect('chat-1');
    stream.options?.onError?.(new Event('error'));
    expect(timers.pending()).toHaveLength(1);

    stream.options?.onStatus?.('connected');
    expect(timers.pending()).toHaveLength(0);
    timers.runPending();
    await Promise.resolve();

    expect(api.getTranscriptCalls).toBe(0);
    expect(projection.snapshot().streamState).toBe('connected');
  });

  it('does not cancel terminal refresh timers when later stream events arrive', async () => {
    const store = new ReadModelEntityStore();
    const timers = timerFixture();
    const stream = streamFixture();
    const api = apiFixture({ transcriptRows: [messageCard('chat-1', 'assistant-1', 'assistant', 'refreshed')] });
    const projection = projectionFixture(store, api, {
      openStream: stream.open,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    });

    projection.connect('chat-1');
    stream.options?.onEvent({
      kind: 'transcript_patch',
      lastEventId: '1',
      payload: { status: rawProgress('chat-1', 'turn-1', { terminal: true }) }
    });
    expect(timers.pending()).toHaveLength(1);

    stream.options?.onStatus?.('connected');
    stream.options?.onEvent({
      kind: 'transcript_append',
      lastEventId: '2',
      payload: { rows: [rawMessageRow('chat-1', 'assistant-2', 'assistant', 'after terminal', 'turn-1')] }
    });
    expect(timers.pending()).toHaveLength(1);

    timers.runPending();
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getTranscriptCalls).toBe(1);
  });

  it('refreshes queue once for a terminal managed turn with assistant output', async () => {
    const store = new ReadModelEntityStore();
    const timers = timerFixture();
    const stream = streamFixture();
    const api = apiFixture({ queuedTurns: [queuedTurn('queued-after-terminal')] });
    const projection = projectionFixture(store, api, {
      openStream: stream.open,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout
    });

    projection.connect('chat-1');
    stream.options?.onEvent({
      kind: 'transcript_snapshot',
      lastEventId: '1',
      payload: {
        rows: [rawMessageRow('chat-1', 'assistant-1', 'assistant', 'done', 'turn-1')],
        status: rawProgress('chat-1', 'turn-1', { terminal: true })
      }
    });
    stream.options?.onEvent({
      kind: 'transcript_patch',
      lastEventId: '2',
      payload: { status: rawProgress('chat-1', 'turn-1', { terminal: true }) }
    });

    expect(timers.pending()).toHaveLength(1);
    timers.runPending();
    await Promise.resolve();

    expect(api.getQueueCalls).toBe(1);
    expect(store.snapshot().pmaQueues['chat-1'].map((turn) => turn.managedTurnId)).toEqual([
      'queued-after-terminal'
    ]);
  });

  it('clears projection data and surfaces an error when the managed thread is missing', async () => {
    const store = new ReadModelEntityStore();
    store.replaceChatTranscript('chat-1', [messageCard('chat-1', 'old', 'assistant', 'old')]);
    store.setPmaQueue('chat-1', [queuedTurn('old-turn')]);
    const missing = missingThreadError();
    const projection = projectionFixture(
      store,
      apiFixture({
        transcriptResult: { ok: false, error: missing },
        queueResult: { ok: false, error: missing }
      })
    );

    await projection.refresh('chat-1');

    expect(store.snapshot().chatTranscripts['chat-1'].order).toEqual([]);
    expect(store.snapshot().pmaQueues['chat-1']).toEqual([]);
    expect(projection.snapshot().activeError).toBe(missing);
  });

  it('closes the stream when progress asks the subscription to close', () => {
    const store = new ReadModelEntityStore();
    const stream = streamFixture();
    const projection = projectionFixture(store, apiFixture(), { openStream: stream.open });

    projection.connect('chat-1');
    stream.options?.onEvent({
      kind: 'transcript_patch',
      lastEventId: '1',
      payload: { status: rawProgress('chat-1', 'turn-1', { stream_should_close: true }) }
    });

    expect(stream.closeCalls).toBe(1);
    expect(projection.snapshot().streamState).toBe('idle');
  });

  it('replaces the owned transcript stream when activating a different chat', async () => {
    const store = new ReadModelEntityStore();
    const stream = streamFixture();
    let streamEligible = true;
    const projection = projectionFixture(store, apiFixture(), {
      openStream: stream.open,
      shouldUseStream: () => streamEligible
    });

    projection.connect('old-running-chat');
    streamEligible = false;
    await projection.activate('new-idle-chat', { quiet: true });

    expect(stream.closeCalls).toBe(1);
    expect(stream.openedChatIds).toEqual(['old-running-chat']);
    stream.options?.onEvent({
      kind: 'transcript_append',
      lastEventId: 'old-1',
      payload: { rows: [rawMessageRow('old-running-chat', 'old-row', 'assistant', 'old', 'turn-old')] }
    });
    expect(store.snapshot().chatTranscripts['old-running-chat']).toBeUndefined();
  });

  it('preserves active errors when deactivating the active chat runtime', async () => {
    const store = new ReadModelEntityStore();
    const stream = streamFixture();
    const missing = missingThreadError();
    const projection = projectionFixture(store, apiFixture(), { openStream: stream.open });

    projection.connect('chat-1');
    projection.replaceState({ activeError: missing, streamState: 'connected' });
    await projection.activate(null);

    expect(stream.closeCalls).toBe(1);
    expect(projection.snapshot().activeError).toBe(missing);
    expect(projection.snapshot().streamState).toBe('idle');
  });

  it('does not churn the transcript stream when reactivating the same chat', async () => {
    const store = new ReadModelEntityStore();
    const stream = streamFixture();
    const projection = projectionFixture(store, apiFixture(), { openStream: stream.open });

    projection.connect('same-chat');
    await projection.activate('same-chat', { quiet: true });

    expect(stream.closeCalls).toBe(0);
    expect(stream.openedChatIds).toEqual(['same-chat']);
  });
});

function projectionFixture(
  store: ReadModelEntityStore,
  api: ReturnType<typeof apiFixture>,
  overrides: Partial<ChatDetailLiveProjectionDeps> = {}
): ChatDetailLiveProjection {
  return new ChatDetailLiveProjection({
    api,
    readModelStore: store,
    getChatSummary: (chatId) => chatSummary(chatId),
    shouldUseStream: () => true,
    now: () => 1_768_176_123_000,
    ...overrides
  });
}

function apiFixture(options: {
  transcriptRows?: ChatTranscriptCard[];
  queuedTurns?: PmaThreadQueue['queuedTurns'];
  transcriptResult?: ApiResult<ChatTranscriptSnapshot>;
  queueResult?: ApiResult<PmaThreadQueue>;
} = {}): ChatDetailLiveProjectionApi & { getTranscriptCalls: number; getQueueCalls: number } {
  return {
    getTranscriptCalls: 0,
    getQueueCalls: 0,
    async getTranscript() {
      this.getTranscriptCalls += 1;
      return options.transcriptResult ?? {
        ok: true,
        data: { rows: options.transcriptRows ?? [], status: null, raw: {} }
      };
    },
    async getQueue() {
      this.getQueueCalls += 1;
      return options.queueResult ?? {
        ok: true,
        data: { managedThreadId: 'chat-1', queueDepth: options.queuedTurns?.length ?? 0, queuedTurns: options.queuedTurns ?? [] }
      };
    }
  };
}

function streamFixture(): {
  options: TranscriptStreamOptions | null;
  closeCalls: number;
  openedChatIds: string[];
  open: (chatId: string, options: TranscriptStreamOptions) => { close: () => void };
} {
  const fixture: {
    options: TranscriptStreamOptions | null;
    closeCalls: number;
    openedChatIds: string[];
    open: (chatId: string, options: TranscriptStreamOptions) => { close: () => void };
  } = {
    options: null,
    closeCalls: 0,
    openedChatIds: [],
    open(chatId: string, options: TranscriptStreamOptions) {
      fixture.openedChatIds.push(chatId);
      fixture.options = options;
      return {
        close: () => {
          fixture.closeCalls += 1;
        }
      };
    }
  };
  return fixture;
}

function timerFixture(): {
  setTimeout: (handler: () => void, delay: number) => number;
  clearTimeout: (id: unknown) => void;
  pending: () => { id: number; handler: () => void; delay: number; cleared: boolean }[];
  runPending: () => void;
} {
  let nextId = 1;
  const timers: { id: number; handler: () => void; delay: number; cleared: boolean }[] = [];
  return {
    setTimeout(handler, delay) {
      const id = nextId++;
      timers.push({ id, handler, delay, cleared: false });
      return id;
    },
    clearTimeout(id) {
      const timer = timers.find((item) => item.id === id);
      if (timer) timer.cleared = true;
    },
    pending() {
      return timers.filter((timer) => !timer.cleared);
    },
    runPending() {
      for (const timer of timers.filter((item) => !item.cleared)) {
        timer.cleared = true;
        timer.handler();
      }
    }
  };
}

function messageCard(
  chatId: string,
  id: string,
  role: 'user' | 'assistant',
  text: string,
  turnId: string | null = id
): ChatTranscriptCard {
  return {
    kind: 'message',
    id,
    turnId,
    orderKey: id,
    timestamp: now,
    message: {
      id,
      chatId,
      role,
      text,
      createdAt: now,
      status: null,
      artifacts: [],
      raw: {}
    }
  };
}

function rawMessageRow(
  chatId: string,
  id: string,
  role: 'user' | 'assistant',
  text: string,
  turnId: string
): Record<string, unknown> {
  return {
    kind: 'message',
    id,
    turn_id: turnId,
    order_key: id,
    timestamp: now,
    message: { id, chat_id: chatId, role, text, created_at: now, artifacts: [] }
  };
}

function rawProgress(chatId: string, turnId: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    managed_thread_id: chatId,
    managed_turn_id: turnId,
    status: 'running',
    stream_should_close: false,
    terminal: false,
    events: [],
    ...overrides
  };
}

function queuedTurn(id: string): PmaThreadQueue['queuedTurns'][number] {
  return {
    managedTurnId: id,
    position: 1,
    state: 'queued',
    prompt: 'prompt',
    promptPreview: 'prompt',
    attachments: [],
    model: null,
    reasoning: null,
    enqueuedAt: now,
    raw: {}
  };
}

function chatSummary(id: string): PmaChatSummary {
  return {
    id,
    title: id,
    lifecycleStatus: null,
    status: 'running',
    agentId: null,
    agentProfile: null,
    model: null,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    isTicketFlow: false,
    progressPercent: null,
    updatedAt: now,
    raw: {}
  };
}

function missingThreadError(): ApiError {
  return {
    kind: 'http',
    status: 404,
    code: 'not_found',
    message: 'Managed thread not found'
  };
}
