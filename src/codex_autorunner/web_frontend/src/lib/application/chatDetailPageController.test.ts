import { describe, expect, it, vi, afterEach } from 'vitest';
import { writable } from 'svelte/store';
import type { ApiError, ApiResult, JsonRecord } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatIndexRow,
  type ProjectionCursor,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot
} from '$lib/api/readModelContracts';
import { CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST, ReadModelEntityStore, type ChatIndexWindowRequest } from '$lib/data';
import type { SurfaceArtifact } from '$lib/viewModels/domain';
import { initialChatDetailSessionState, type ChatDetailSessionState } from './chatDetailSession';
import {
  ChatDetailPageController,
  type ChatDetailPageIndexSession,
  type ChatDetailPageSupportData
} from './chatDetailPageController';

const issuedAt = '2026-05-20T12:00:00.000Z';

afterEach(() => {
  vi.useRealTimers();
});

describe('ChatDetailPageController', () => {
  it('starts the index session, renders cached rows immediately, and loads supporting data', async () => {
    const harness = createHarness();
    harness.store.applyChatIndexSnapshot(chatIndexSnapshot([chatIndexRow('chat-1')]));

    harness.controller.mount({
      route: route(),
      currentRequest: { filter: 'all', limit: 50 },
      ticketRunGroupRequest: CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(harness.session.activate).toHaveBeenCalledWith({
      primaryRequest: { filter: 'all', limit: 50 },
      companionRequests: [CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST],
      refresh: false
    });
    expect(harness.session.start).toHaveBeenCalledTimes(1);
    expect(harness.loadingChats.at(-1)).toBe(false);
    expect(harness.sessionState.activeChatId).toBe('chat-1');
    expect(harness.liveProjection.activate).toHaveBeenCalledWith('chat-1', { quiet: false });
    expect(harness.supportData.at(-1)?.agents).toEqual([{ id: 'codex' }]);
  });

  it('still applies support-data side effects when agent loading fails', async () => {
    const createInitialDraft = vi.fn();
    const harness = createHarness({
      onCreateInitialDraft: createInitialDraft,
      supportApi: {
        ...supportApi(),
        listAgents: async (): Promise<ApiResult<{ agents: JsonRecord[]; defaults: JsonRecord; default: string }>> =>
          ({ ok: false, error: { kind: 'http', status: 500, code: 'agents_failed', message: 'agents failed' } })
      }
    });

    harness.controller.mount({
      route: { ...route(), searchParams: new URLSearchParams('draft=hello') },
      currentRequest: { filter: 'all', limit: 50 },
      ticketRunGroupRequest: CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(harness.supportData.at(-1)).toMatchObject({
      agents: [],
      defaults: {},
      defaultAgent: 'codex'
    });
    expect(createInitialDraft).toHaveBeenCalledOnce();
  });

  it('activates a route replacement and delegates active transcript runtime to the live projection', () => {
    const harness = createHarness();

    harness.controller.setRoute(route('deep-linked-chat'));

    expect(harness.sessionState.activeChatId).toBe('deep-linked-chat');
    expect(harness.sessionState.loadingActive).toBe(true);
    expect(harness.liveProjection.activate).toHaveBeenCalledWith('deep-linked-chat', { quiet: false });
  });

  it('keeps a route-selected chat active when read-model publication runs synchronously', async () => {
    const harness = createHarness();
    harness.store.applyChatIndexSnapshot(chatIndexSnapshot([
      chatIndexRow('previous-chat'),
      chatIndexRow('selected-chat')
    ]));
    harness.controller.mount({
      route: route('previous-chat'),
      currentRequest: { filter: 'all', limit: 50 },
      ticketRunGroupRequest: CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(harness.sessionState.activeChatId).toBe('previous-chat');
    harness.liveProjection.activate.mockClear();

    harness.controller.setRoute(route('selected-chat'));

    expect(harness.sessionState.activeChatId).toBe('selected-chat');
    expect(harness.liveProjection.activate).toHaveBeenCalledWith('selected-chat', { quiet: false });
  });

  it('debounces filter refreshes through the chat index session', () => {
    vi.useFakeTimers();
    const harness = createHarness();
    const request: ChatIndexWindowRequest = { filter: 'archived', query: 'old', limit: 50 };

    harness.controller.setIndexRequest(request);
    expect(harness.session.activate).not.toHaveBeenCalled();
    vi.advanceTimersByTime(180);

    expect(harness.session.activate).toHaveBeenCalledWith({ primaryRequest: request });
  });

  it('selects the active chat replacement when the current chat is archived out of the active window', () => {
    const harness = createHarness();
    harness.controller.mount({
      route: route(),
      currentRequest: { filter: 'all', limit: 50 },
      ticketRunGroupRequest: CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST
    });
    harness.store.applyChatIndexSnapshot(chatIndexSnapshot([
      chatIndexRow('active', { primarySurface: { surfaceKind: 'managed_thread', surfaceKey: 'scope-1' } })
    ]));
    harness.liveProjection.activate.mockClear();

    harness.store.applyChatIndexSnapshot(chatIndexSnapshot([
      chatIndexRow('active', {
        status: 'archived',
        primarySurface: { surfaceKind: 'managed_thread', surfaceKey: 'scope-1' }
      }),
      chatIndexRow('replacement', { primarySurface: { surfaceKind: 'managed_thread', surfaceKey: 'scope-1' } })
    ]));

    expect(harness.sessionState.activeChatId).toBe('replacement');
    expect(harness.liveProjection.activate).toHaveBeenCalledWith('replacement', { quiet: false });
  });

  it('owns active-clock lifecycle and teardown', () => {
    vi.useFakeTimers();
    const harness = createHarness({ now: vi.fn(() => 1000) });

    harness.controller.setProgressStatus('running');
    expect(harness.clockTicks).toEqual([1000]);
    vi.mocked(harness.now).mockReturnValue(2000);
    vi.advanceTimersByTime(1000);
    expect(harness.clockTicks).toEqual([1000, 2000]);

    harness.controller.destroy();
    vi.mocked(harness.now).mockReturnValue(3000);
    vi.advanceTimersByTime(1000);

    expect(harness.clockTicks).toEqual([1000, 2000]);
    expect(harness.session.stop).toHaveBeenCalledTimes(1);
    expect(harness.session.activate).toHaveBeenLastCalledWith({ companionRequests: [], refresh: false });
    expect(harness.liveProjection.close).toHaveBeenCalledTimes(1);
  });
});

function createHarness(options: {
  now?: () => number;
  supportApi?: ReturnType<typeof supportApi>;
  onCreateInitialDraft?: () => void;
} = {}) {
  const store = new ReadModelEntityStore();
  let sessionState: ChatDetailSessionState = initialChatDetailSessionState();
  const indexState = writable({ status: 'idle' as const, error: null as ApiError | null });
  const session = {
    state: indexState,
    activate: vi.fn(async () => undefined),
    start: vi.fn(),
    stop: vi.fn(),
    refresh: vi.fn(async () => undefined),
    loadMore: vi.fn(async () => undefined),
    setCompanionRequests: vi.fn()
  } satisfies ChatDetailPageIndexSession;
  const liveProjection = {
    activate: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    retry: vi.fn(),
    close: vi.fn()
  };
  const loadingChats: boolean[] = [];
  const supportData: ChatDetailPageSupportData[] = [];
  const clockTicks: number[] = [];
  const now = options.now ?? vi.fn(() => Date.now());
  const controller = new ChatDetailPageController({
    readModelStore: store,
    chatIndexSession: session,
    liveProjection,
    supportApi: options.supportApi ?? supportApi(),
    readSessionState: () => sessionState,
    writeSessionState: (state) => {
      sessionState = state;
    },
    onReadModelState: vi.fn(),
    onLoadingChats: (value) => loadingChats.push(value),
    onChatError: vi.fn(),
    onFilterArchived: vi.fn(),
    onClockTick: (value) => clockTicks.push(value),
    onPinnedChatsLoaded: vi.fn(),
    onInitialDraft: vi.fn(),
    onCreateInitialDraft: options.onCreateInitialDraft ?? vi.fn(),
    onSupportDataLoaded: (data) => supportData.push(data),
    onSyncSelectors: vi.fn(),
    onMarkRead: vi.fn(),
    timers: { now }
  });
  return { controller, store, session, liveProjection, loadingChats, supportData, clockTicks, now, get sessionState() { return sessionState; } };
}

function route(chatId?: string) {
  return {
    chatId,
    searchParams: new URLSearchParams(),
    data: { chatId, activeDetail: null, chatIndex: null }
  };
}

function supportApi() {
  return {
    listFiles: async (): Promise<ApiResult<SurfaceArtifact[]>> => ok([]),
    listAgents: async (): Promise<ApiResult<{ agents: JsonRecord[]; defaults: JsonRecord; default: string }>> =>
      ok({ agents: [{ id: 'codex' }], defaults: { agent: 'codex', profile: '' }, default: 'codex' }),
    repoWorktreeTopology: async (): Promise<ApiResult<RepoWorktreeTopologySnapshot>> => ok({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'repo_worktree.topology.snapshot',
      cursor: projectionCursor(),
      window: windowInfo(),
      repos: [],
      worktrees: [],
      repair: repairPolicy()
    }),
    repoWorktreeRuntime: async (): Promise<ApiResult<RepoWorktreeRuntimeSnapshot>> => ok({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'repo_worktree.runtime.snapshot',
      cursor: projectionCursor(),
      window: windowInfo(),
      runtime: [],
      repair: repairPolicy()
    })
  };
}

function chatIndexSnapshot(rows: ChatIndexRow[]) {
  return {
    cursor: projectionCursor(),
    rows,
    groups: [],
    counters: {
      total: rows.length,
      waiting: 0,
      running: rows.filter((row) => row.status === 'running').length,
      unread: 0,
      archived: rows.filter((row) => row.status === 'archived').length
    }
  };
}

function chatIndexRow(id: string, overrides: Partial<ChatIndexRow> = {}): ChatIndexRow {
  return {
    chatId: id,
    surface: 'pma',
    title: id,
    status: 'running',
    unreadCount: 0,
    lastActivityAt: issuedAt,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    runId: null,
    agent: null,
    agentProfile: null,
    chatKind: null,
    model: null,
    groupId: null,
    ...overrides
  };
}

function projectionCursor(sequence = 1): ProjectionCursor {
  return { value: `test:${sequence}`, sequence, source: 'test', issuedAt };
}

function windowInfo() {
  return { limit: 50, nextCursor: null, previousCursor: null, totalEstimate: 0, totalIsExact: true };
}

function repairPolicy() {
  return {
    snapshotRoute: '/hub/read-models/test',
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}
