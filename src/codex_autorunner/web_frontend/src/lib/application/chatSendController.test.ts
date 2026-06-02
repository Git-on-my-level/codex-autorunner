import { describe, expect, it, vi } from 'vitest';
import type { ApiError, ChatQueuedTurn } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import { localChatScopeOption, type PendingAttachment } from '$lib/viewModels/chat';
import type { ChatSummary, ChatRunProgress } from '$lib/viewModels/domain';
import { initialChatDetailSessionState, type ChatDetailSessionState } from './chatDetailSession';
import { createChatSendController, type ChatSendControllerApi } from './chatSendController';

const now = new Date('2026-05-16T12:00:00.000Z');

describe('chat send controller', () => {
  it('sends an idle chat with a pending transcript card until backend confirmation', async () => {
    const harness = createHarness({ draft: 'hello' });
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-1', text: 'hello' }));

    await harness.controller.sendMessage();

    expect(harness.api.pma.sendMessage).toHaveBeenCalledWith(
      'chat-1',
      expect.objectContaining({ message: 'hello', busy_policy: undefined, client_turn_id: 'optimistic:user:1778932800000:turn' })
    );
    expect(harness.state.draft).toBe('');
    expect(harness.transcriptIds('chat-1')).toEqual(['optimistic:user:1778932800000:turn']);
  });

  it('renders running-chat follow-ups only in the optimistic queue', async () => {
    const harness = createHarness({
      draft: 'follow up',
      progress: { ...progress(), status: 'running' }
    });
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-2', text: 'follow up' }));
    harness.refreshActive.mockImplementation(async (chatId) => {
      harness.store.setChatQueue(chatId, [queuedTurn('turn-2', { client_turn_id: 'optimistic:user:1778932800000:turn' })]);
    });

    await harness.controller.sendMessage();

    expect(harness.api.pma.sendMessage).toHaveBeenCalledWith(
      'chat-1',
      expect.objectContaining({ busy_policy: 'queue' })
    );
    expect(harness.transcriptIds('chat-1')).toEqual([]);
    expect(harness.queueIds('chat-1')).toEqual(['turn-2']);
  });

  it('keeps explicit interrupt out of the optimistic queue', async () => {
    const harness = createHarness({
      draft: 'replace current work',
      progress: { ...progress(), status: 'running' }
    });
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-2', text: 'replace current work' }));

    await harness.controller.sendMessage('interrupt');

    expect(harness.api.pma.sendMessage).toHaveBeenCalledWith(
      'chat-1',
      expect.objectContaining({ busy_policy: 'interrupt' })
    );
    expect(harness.queueIds('chat-1')).toEqual([]);
    expect(harness.transcriptIds('chat-1')).toEqual(['optimistic:user:1778932800000:turn']);
  });

  it('removes an idle optimistic transcript card when the backend queued the turn', async () => {
    const harness = createHarness({ draft: 'surprise queue' });
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-2', text: 'surprise queue' }));
    harness.refreshActive.mockImplementation(async (chatId) => {
      harness.store.setChatQueue(chatId, [queuedTurn('turn-2', { client_turn_id: 'optimistic:user:1778932800000:turn' })]);
    });

    await harness.controller.sendMessage();

    expect(harness.transcriptIds('chat-1')).toEqual([]);
    expect(harness.queueIds('chat-1')).toEqual(['turn-2']);
  });

  it('restores draft and attachments when upload fails', async () => {
    const attachment = pendingAttachment('att-1', 'notes.md') as PendingAttachment & { file: File };
    attachment.file = new File(['notes'], 'notes.md');
    const harness = createHarness({ draft: 'with file', attachments: [attachment] });
    harness.api.pma.uploadInboxFile.mockResolvedValue(error('upload_failed'));

    await harness.controller.sendMessage();

    expect(harness.api.pma.sendMessage).not.toHaveBeenCalled();
    expect(harness.state.draft).toBe('with file');
    expect(harness.state.attachments).toEqual([attachment]);
    expect(harness.state.composeError?.code).toBe('upload_failed');
  });

  it('starts the first local draft send with the client-stable chat id', async () => {
    const draftChat = chatSummary('pma:11111111-1111-4111-8111-111111111111', { lifecycleStatus: 'draft', title: 'Draft' });
    const harness = createHarness({
      activeChatId: draftChat.id,
      activeChat: draftChat,
      localDraftChat: draftChat,
      draft: 'first message'
    });
    harness.api.pma.startChatWithMessage.mockResolvedValue(ok({ chatId: draftChat.id, id: 'turn-1', text: 'first message' }));

    await harness.controller.sendMessage();

    expect(harness.api.pma.startChatWithMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        managed_thread_id: draftChat.id,
        message: 'first message',
        client_turn_id: 'optimistic:user:1778932800000:turn'
      })
    );
    expect(harness.refreshActive).toHaveBeenCalledWith(draftChat.id, { quiet: true, forceStream: true });
    expect(harness.state.session.activeChatId).toBe(draftChat.id);
    expect(harness.transcriptIds(draftChat.id)).toEqual(['optimistic:user:1778932800000:turn']);
  });

  it('cancels a queued turn and refreshes the queue', async () => {
    const turn = queuedTurn('turn-queued');
    const harness = createHarness({ queuedTurns: [turn] });
    harness.api.pma.cancelQueuedTurn.mockResolvedValue(ok({}));

    await harness.controller.cancelQueuedTurn(turn, { confirmed: true });

    expect(harness.api.pma.cancelQueuedTurn).toHaveBeenCalledWith('chat-1', 'turn-queued');
    expect(harness.queueIds('chat-1')).toEqual([]);
    expect(harness.refreshActive).toHaveBeenCalledWith('chat-1', { quiet: true });
  });

  it('interrupts with a queued turn by canceling it, then sending with interrupt policy', async () => {
    const turn = queuedTurn('turn-queued');
    const harness = createHarness({ queuedTurns: [turn] });
    harness.api.pma.cancelQueuedTurn.mockResolvedValue(ok({}));
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-new', text: turn.prompt }));

    await harness.controller.interruptWithQueuedTurn(turn);

    expect(harness.queueIds('chat-1')).toEqual([]);
    expect(harness.transcriptIds('chat-1')).toEqual(['optimistic:user:1778932800000:turn']);
    expect(harness.api.pma.sendMessage).toHaveBeenCalledWith(
      'chat-1',
      expect.objectContaining({ busy_policy: 'interrupt', message: turn.prompt })
    );
  });

  it('removes queued-turn interrupt optimistic rows after backend transcript confirmation', async () => {
    const turn = queuedTurn('turn-queued');
    const harness = createHarness({ queuedTurns: [turn] });
    harness.store.upsertChatTranscriptCards('chat-1', [
      userTranscriptCard('optimistic:user:unrelated', 'still pending', {
        optimistic: true,
        client_turn_id: 'optimistic:user:unrelated',
        correlation_id: 'optimistic:user:unrelated'
      })
    ]);
    harness.api.pma.cancelQueuedTurn.mockResolvedValue(ok({}));
    harness.api.pma.sendMessage.mockResolvedValue(ok({ chatId: 'chat-1', id: 'turn-new', text: turn.prompt }));
    harness.refreshActive.mockImplementation(async (chatId) => {
      harness.store.upsertChatTranscriptCards(chatId, [
        userTranscriptCard('turn-new:user', turn.prompt, {
          identity: { correlation_id: 'optimistic:user:1778932800000:turn' }
        })
      ]);
    });

    await harness.controller.interruptWithQueuedTurn(turn);

    expect(harness.transcriptIds('chat-1')).toEqual(['optimistic:user:unrelated', 'turn-new:user']);
  });

  it('clears the real queue but ignores optimistic-only queue rows', async () => {
    const realTurn = queuedTurn('turn-real');
    const optimisticTurn = queuedTurn('optimistic-queue:client-1', { optimistic: true });
    const harness = createHarness({ queuedTurns: [realTurn, optimisticTurn] });
    harness.api.pma.clearQueue.mockResolvedValue(ok({}));

    await harness.controller.clearQueue();

    expect(harness.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Clear queue' }));
    expect(harness.api.pma.clearQueue).toHaveBeenCalledWith('chat-1');
    expect(harness.queueIds('chat-1')).toEqual([]);
  });
});

function createHarness(options: {
  activeChatId?: string;
  activeChat?: ChatSummary;
  localDraftChat?: ChatSummary | null;
  draft?: string;
  attachments?: PendingAttachment[];
  progress?: ChatRunProgress | null;
  queuedTurns?: ChatQueuedTurn[];
} = {}) {
  const store = new ReadModelEntityStore();
  const activeChatId = options.activeChatId ?? 'chat-1';
  const activeChat = options.activeChat ?? chatSummary(activeChatId);
  if (options.queuedTurns) store.setChatQueue(activeChatId, options.queuedTurns);
  const state = {
    activeChatId,
    activeChat,
    localDraftChat: options.localDraftChat ?? null,
    draft: options.draft ?? '',
    attachments: options.attachments ?? [],
    progress: options.progress ?? null,
    session: {
      ...initialChatDetailSessionState(),
      activeChatId,
      localDraftChat: options.localDraftChat ?? null
    } as ChatDetailSessionState,
    sending: false,
    composeError: null as ApiError | null
  };
  const api = {
    pma: {
      sendMessage: vi.fn(),
      startChatWithMessage: vi.fn(),
      createChat: vi.fn(),
      forkThread: vi.fn(),
      uploadInboxFile: vi.fn(),
      cancelQueuedTurn: vi.fn(),
      clearQueue: vi.fn()
    }
  } as unknown as MockedApi;
  api.pma.uploadInboxFile.mockResolvedValue(ok(['uploaded.md']));
  const refreshActive = vi.fn(async (_chatId: string, _options: { quiet?: boolean; forceStream?: boolean }) => {});
  const confirm = vi.fn(async () => true);
  const controller = createChatSendController({
    api: api as ChatSendControllerApi,
    readModelStore: store,
    getActiveChatId: () => state.activeChatId,
    getActiveChat: () => state.activeChat,
    getDisplayedProgress: () => state.progress,
    getDraft: () => state.draft,
    setDraft: (value) => {
      state.draft = value;
    },
    getPendingAttachments: () => state.attachments,
    setPendingAttachments: (value) => {
      state.attachments = value;
    },
    getComposerEditVersion: () => 0,
    getSelectedScope: () => localChatScopeOption(),
    getSelectedScopeSource: () => 'default_hub',
    getSelectedAgent: () => 'codex',
    getSelectedProfile: () => '',
    getSelectedModel: () => '',
    getSelectedReasoning: () => '',
    getNewChatKind: () => 'pma',
    canStartCodingAgentChat: () => false,
    newChatDisplayName: () => 'New chat',
    readSessionState: () => state.session,
    writeSessionState: (next) => {
      state.session = next;
      state.activeChatId = next.activeChatId ?? state.activeChatId;
    },
    getLocalDraftChat: () => state.localDraftChat,
        invalidateChatMutation: vi.fn(async () => {}),
    refreshActive,
    setSending: (value) => {
      state.sending = value;
    },
    setComposeError: (value) => {
      state.composeError = value;
    },
    confirm,
    now: () => now,
    randomId: () => 'turn'
  });
  return {
    api,
    store,
    state,
    controller,
    confirm,
    refreshActive,
    transcriptIds: (chatId: string) => store.snapshot().chatTranscripts[chatId]?.order ?? [],
    queueIds: (chatId: string) => (store.snapshot().chatQueues[chatId] ?? []).map((turn) => turn.managedTurnId)
  };
}

type MockedApi = {
  pma: {
    [K in keyof ChatSendControllerApi['pma']]: ReturnType<typeof vi.fn>;
  };
};

function ok<T>(data: T) {
  return { ok: true as const, data };
}

function error(code: string) {
  return {
    ok: false as const,
    error: { kind: 'http' as const, status: 500, code, message: code, details: null }
  };
}

function chatSummary(id: string, overrides: Partial<ChatSummary> = {}): ChatSummary {
  return {
    id,
    title: 'Chat',
    lifecycleStatus: 'active',
    status: 'idle',
    agentId: 'codex',
    chatKind: 'pma',
    agentProfile: null,
    model: null,
    repoId: null,
    worktreeId: null,
    ticketId: null,
    isTicketFlow: false,
    progressPercent: null,
    updatedAt: now.toISOString(),
    raw: {},
    ...overrides
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
    enqueuedAt: now.toISOString(),
    raw
  };
}

function userTranscriptCard(id: string, text: string, raw: Record<string, unknown>) {
  return {
    kind: 'message' as const,
    id,
    turnId: id.split(':')[1] ?? null,
    orderKey: `00000001|${now.toISOString()}|${id}`,
    timestamp: now.toISOString(),
    message: {
      id,
      chatId: 'chat-1',
      role: 'user' as const,
      text,
      createdAt: now.toISOString(),
      status: null,
      artifacts: [],
      raw
    }
  };
}

function pendingAttachment(
  id: string,
  title: string,
  extra: Partial<PendingAttachment> = {}
): PendingAttachment {
  return {
    id,
    kind: 'file',
    title,
    url: null,
    sizeLabel: null,
    uploadedName: null,
    uploadState: 'pending',
    ...extra
  };
}

function progress(): ChatRunProgress {
  return {
    id: 'turn-running',
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
    elapsedSeconds: 0,
    startedAt: now.toISOString(),
    idleSeconds: null,
    lastEventId: null,
    lastEventAt: null,
    progressPercent: null,
    events: [],
    raw: {}
  };
}
