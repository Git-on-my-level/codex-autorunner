import type { ApiError, PmaQueuedTurn, WebApiClient } from '$lib/api/client';
import type { ReadModelEntityState, ReadModelEntityStore } from '$lib/data/readModelStore';
import { selectPmaQueue } from '$lib/data/readModelViewModels';
import type { PmaRunProgress, PmaChatSummary } from '$lib/viewModels/domain';
import {
  composeMessageWithAttachments,
  type DocumentFileIntentPayload,
  type PendingAttachment,
  type PmaChatScopeOption,
  type PmaChatScopeSource
} from '$lib/viewModels/pmaChat';
import {
  buildOptimisticQueuedTurn,
  buildOptimisticUserTranscriptCard,
  chatTranscriptHasInFlightOptimisticTurn,
  isOptimisticQueuedTurn,
  queueContainsCommittedClientTurn,
  transcriptContainsCommittedUserRow,
  withoutOptimisticQueuedTurn
} from './pmaChatArchitecture';
import {
  executePmaChatCommandPlan,
  planInterruptExistingChat,
  planQueueExistingChat,
  planSendExistingChat,
  planStartAndSendChat
} from './pmaChatCommands';
import { commitLocalDraftChat, isLocalDraftChatId, type ChatDetailSessionState } from './chatDetailSession';

export type ChatSendBusyPolicy = 'queue' | 'interrupt' | null;

export type ChatSendControllerApi = {
  pma: Pick<
    WebApiClient['pma'],
    | 'sendMessage'
    | 'startChatWithMessage'
    | 'createChat'
    | 'forkThread'
    | 'uploadInboxFile'
    | 'cancelQueuedTurn'
    | 'clearQueue'
  >;
};

export type ChatSendControllerDeps = {
  api: ChatSendControllerApi;
  readModelStore: Pick<
    ReadModelEntityStore,
    | 'snapshot'
    | 'optimisticSend'
    | 'upsertChatTranscriptCards'
    | 'removeOptimisticChatTranscriptCards'
    | 'failOptimisticMutation'
    | 'setPmaQueue'
  >;
  getActiveChatId: () => string | null;
  getActiveChat: () => PmaChatSummary | null;
  getDisplayedProgress: () => PmaRunProgress | null;
  getDraft: () => string;
  setDraft: (value: string, chatId?: string | null) => void;
  getPendingAttachments: () => PendingAttachment[];
  setPendingAttachments: (value: PendingAttachment[], chatId?: string | null) => void;
  getComposerEditVersion: () => number;
  getSelectedScope: () => PmaChatScopeOption;
  getSelectedScopeSource: () => PmaChatScopeSource;
  getSelectedAgent: () => string;
  getSelectedProfile: () => string;
  getSelectedModel: () => string;
  getSelectedReasoning: () => string;
  getNewChatKind: () => 'pma' | 'agent';
  canStartCodingAgentChat: () => boolean;
  newChatDisplayName: () => string;
  readSessionState: () => ChatDetailSessionState;
  writeSessionState: (state: ChatDetailSessionState) => void;
  getLocalDraftChat: () => PmaChatSummary | null;
  syncDetailUrl: (chatId: string) => Promise<void>;
  invalidateChatMutation: (chatId: string) => Promise<void>;
  refreshActive: (chatId: string, options: { quiet?: boolean }) => Promise<void>;
  setSending: (value: boolean) => void;
  setComposeError: (error: ApiError | null) => void;
  confirm: (options: ChatSendConfirmationOptions) => Promise<boolean>;
  now?: () => Date;
  randomId?: () => string;
};

export type ChatSendConfirmationOptions = {
  title: string;
  message: string;
  confirmText: string;
  danger?: boolean;
};

export type ChatSendController = {
  sendMessage: (busyPolicy?: ChatSendBusyPolicy) => Promise<void>;
  interruptWithDraft: (canInterrupt: boolean) => Promise<void>;
  cancelQueuedTurn: (turn: PmaQueuedTurn, options?: { confirmed?: boolean }) => Promise<void>;
  interruptWithQueuedTurn: (turn: PmaQueuedTurn) => Promise<void>;
  clearQueue: (options?: { confirmed?: boolean }) => Promise<boolean>;
};

export function createChatSendController(deps: ChatSendControllerDeps): ChatSendController {
  const now = () => (deps.now ? deps.now() : new Date());
  const randomId = () => deps.randomId?.() ?? Math.random().toString(36).slice(2, 8);
  const isoNow = () => now().toISOString();
  const optimisticClientTurnId = () => `optimistic:user:${now().getTime()}:${randomId()}`;

  function selectQueue(chatId: string): PmaQueuedTurn[] {
    return selectPmaQueue(deps.readModelStore.snapshot() as ReadModelEntityState, chatId);
  }

  function pushOptimisticQueuedTurn(chatId: string, text: string, clientTurnId: string): void {
    const current = selectQueue(chatId);
    deps.readModelStore.setPmaQueue(chatId, [
      ...current,
      buildOptimisticQueuedTurn(text, clientTurnId, current.length + 1, isoNow())
    ]);
  }

  function turnLandedInQueue(chatId: string, clientTurnId: string): boolean {
    return queueContainsCommittedClientTurn(selectQueue(chatId), clientTurnId);
  }

  function removeOptimisticQueuedTurn(chatId: string, clientTurnId: string): void {
    const current = selectQueue(chatId);
    const next = withoutOptimisticQueuedTurn(current, clientTurnId);
    if (next.length === current.length) return;
    deps.readModelStore.setPmaQueue(chatId, next);
  }

  function transcriptHasBackendUserRow(chatId: string, clientTurnId: string): boolean {
    return transcriptContainsCommittedUserRow(
      (deps.readModelStore.snapshot() as ReadModelEntityState).chatTranscripts[chatId],
      clientTurnId
    );
  }

  async function uploadAttachments(attachments: PendingAttachment[]): Promise<PendingAttachment[] | null> {
    const uploaded: PendingAttachment[] = [];
    for (const attachment of attachments) {
      const file = (attachment as PendingAttachment & { file?: File }).file;
      if (!file || attachment.uploadedName) {
        uploaded.push(attachment);
        continue;
      }
      const result = await deps.api.pma.uploadInboxFile(file);
      if (!result.ok || !result.data[0]) {
        deps.setComposeError(
          result.ok
            ? { kind: 'parse', status: null, code: 'upload_missing_file', message: 'Upload did not return a file name.' }
            : result.error
        );
        return null;
      }
      const uploadedName = result.data[0];
      uploaded.push({
        ...attachment,
        uploadedName,
        url: `/hub/pma/files/inbox/${encodeURIComponent(uploadedName)}`,
        uploadState: 'uploaded'
      });
    }
    return uploaded;
  }

  async function sendMessage(busyPolicy: ChatSendBusyPolicy = null): Promise<void> {
    const activeChatId = deps.getActiveChatId();
    const draftSnapshot = deps.getDraft();
    const attachmentsSnapshot = deps.getPendingAttachments();
    if ((!draftSnapshot.trim() && attachmentsSnapshot.length === 0) || !activeChatId) return;

    const optimisticChatId = activeChatId;
    const transcript = (deps.readModelStore.snapshot() as ReadModelEntityState).chatTranscripts[optimisticChatId];
    const hasInFlightOptimisticTurn = chatTranscriptHasInFlightOptimisticTurn(transcript);
    const displayedProgress = deps.getDisplayedProgress();
    const willQueueOptimistically =
      !isLocalDraftChatId(optimisticChatId) &&
      busyPolicy !== 'interrupt' &&
      (busyPolicy === 'queue' || displayedProgress?.status === 'running' || hasInFlightOptimisticTurn);
    const optimisticId = optimisticClientTurnId();
    const optimisticTimestamp = isoNow();
    const optimisticPlaceholder = buildOptimisticUserTranscriptCard(
      optimisticChatId,
      draftSnapshot,
      optimisticId,
      optimisticTimestamp,
      attachmentsSnapshot
    );

    if (willQueueOptimistically) {
      pushOptimisticQueuedTurn(optimisticChatId, draftSnapshot, optimisticId);
    } else {
      deps.readModelStore.optimisticSend(
        optimisticChatId,
        {
          itemId: optimisticId,
          kind: 'user_message',
          role: 'user',
          createdAt: optimisticTimestamp,
          text: draftSnapshot,
          artifactIds: [],
          clientMessageId: optimisticId
        },
        optimisticId
      );
      deps.readModelStore.upsertChatTranscriptCards(optimisticChatId, [optimisticPlaceholder]);
    }

    deps.setDraft('', activeChatId);
    deps.setPendingAttachments([], activeChatId);
    const composerVersionAtClear = deps.getComposerEditVersion();
    deps.setSending(true);
    deps.setComposeError(null);

    const moveOptimisticToCommittedChat = (committedChatId: string) => {
      if (committedChatId === optimisticChatId) return;
      deps.readModelStore.upsertChatTranscriptCards(committedChatId, [
        {
          ...optimisticPlaceholder,
          message: { ...optimisticPlaceholder.message, chatId: committedChatId }
        }
      ]);
      deps.readModelStore.removeOptimisticChatTranscriptCards(optimisticChatId);
    };
    const removeOptimistic = (chatId = optimisticChatId, options: { requireBackendRow?: boolean } = {}) => {
      if (options.requireBackendRow && !transcriptHasBackendUserRow(chatId, optimisticId)) return false;
      deps.readModelStore.removeOptimisticChatTranscriptCards(chatId);
      return true;
    };
    const restoreDraft = () => {
      if (willQueueOptimistically) removeOptimisticQueuedTurn(optimisticChatId, optimisticId);
      else {
        removeOptimistic();
        deps.readModelStore.failOptimisticMutation(optimisticId);
      }
      if (deps.getComposerEditVersion() !== composerVersionAtClear) return;
      deps.setDraft(draftSnapshot, optimisticChatId);
      deps.setPendingAttachments(attachmentsSnapshot, optimisticChatId);
    };

    const uploaded = await uploadAttachments(attachmentsSnapshot);
    if (!uploaded) {
      restoreDraft();
      deps.setSending(false);
      return;
    }

    const targetChatId = deps.getActiveChatId();
    if (!targetChatId) {
      restoreDraft();
      deps.setSending(false);
      return;
    }
    const activeChat = deps.getActiveChat();
    const attachmentsForMessage = uploaded;
    const message = composeMessageWithAttachments(draftSnapshot, attachmentsForMessage);
    const targetIsDraft = isLocalDraftChatId(targetChatId);
    const targetIsRunning = deps.getDisplayedProgress()?.status === 'running';
    const profileForSend = activeChat?.agentProfile?.trim() || deps.getSelectedProfile().trim() || '';
    const commandPlan = targetIsDraft
      ? planStartAndSendChat(
          deps.getSelectedScope(),
          deps.getSelectedAgent(),
          deps.getSelectedProfile(),
          deps.getSelectedModel(),
          message,
          {
            name: activeChat?.title || deps.newChatDisplayName(),
            chatKind: deps.getNewChatKind() === 'agent' && deps.canStartCodingAgentChat() ? 'coding_agent' : 'pma',
            attachments: attachmentsForMessage,
            reasoning: deps.getSelectedReasoning(),
            clientTurnId: optimisticId,
            scopeSource: deps.getSelectedScopeSource()
          }
        )
      : busyPolicy === 'interrupt'
        ? planInterruptExistingChat(targetChatId, message, {
            model: deps.getSelectedModel(),
            attachments: attachmentsForMessage,
            reasoning: deps.getSelectedReasoning(),
            profile: profileForSend,
            clientTurnId: optimisticId
          })
        : busyPolicy === 'queue' || targetIsRunning
          ? planQueueExistingChat(targetChatId, message, {
              model: deps.getSelectedModel(),
              attachments: attachmentsForMessage,
              reasoning: deps.getSelectedReasoning(),
              profile: profileForSend,
              clientTurnId: optimisticId
            })
          : planSendExistingChat(targetChatId, message, {
              model: deps.getSelectedModel(),
              attachments: attachmentsForMessage,
              reasoning: deps.getSelectedReasoning(),
              profile: profileForSend,
              clientTurnId: optimisticId
            });

    const result = await executePmaChatCommandPlan(deps.api, commandPlan);
    if (result.ok) {
      const committedChatId = targetIsDraft ? result.data.chatId : targetChatId;
      if (!committedChatId) {
        removeOptimistic();
        deps.setComposeError({
          kind: 'parse',
          status: null,
          code: 'missing_chat_id',
          message: 'Started chat response did not include a managed thread id.'
        });
        deps.setSending(false);
        return;
      }
      if (targetIsDraft) {
        const localDraftChat = deps.getLocalDraftChat();
        const draftChatForPlaceholder =
          localDraftChat?.id === targetChatId
            ? localDraftChat
            : activeChat && activeChat.id === targetChatId
              ? activeChat
              : null;
        deps.writeSessionState(
          commitLocalDraftChat(deps.readSessionState(), draftChatForPlaceholder, committedChatId, optimisticTimestamp)
        );
        moveOptimisticToCommittedChat(committedChatId);
        await deps.syncDetailUrl(committedChatId);
      }
      await deps.invalidateChatMutation(committedChatId);
      await deps.refreshActive(committedChatId, { quiet: true });
      if (willQueueOptimistically) {
        removeOptimisticQueuedTurn(committedChatId, optimisticId);
      } else if (turnLandedInQueue(committedChatId, optimisticId)) {
        removeOptimistic(committedChatId);
        deps.readModelStore.failOptimisticMutation(optimisticId);
      } else {
        removeOptimistic(committedChatId, { requireBackendRow: true });
      }
    } else {
      restoreDraft();
      deps.setComposeError(result.error);
    }
    deps.setSending(false);
  }

  async function cancelQueuedTurn(turn: PmaQueuedTurn, options: { confirmed?: boolean } = {}): Promise<void> {
    const activeChatId = deps.getActiveChatId();
    if (!activeChatId || !turn.managedTurnId || isOptimisticQueuedTurn(turn)) return;
    if (!options.confirmed) {
      const ok = await deps.confirm({
        title: 'Cancel queued message',
        message: `Cancel queued message ${turn.position}?`,
        confirmText: 'Cancel message',
        danger: true
      });
      if (!ok) return;
    }
    deps.setComposeError(null);
    const result = await deps.api.pma.cancelQueuedTurn(activeChatId, turn.managedTurnId);
    if (result.ok) {
      deps.readModelStore.setPmaQueue(
        activeChatId,
        selectQueue(activeChatId).filter((item) => item.managedTurnId !== turn.managedTurnId)
      );
      await deps.refreshActive(activeChatId, { quiet: true });
    } else {
      deps.setComposeError(result.error);
    }
  }

  async function interruptWithQueuedTurn(turn: PmaQueuedTurn): Promise<void> {
    const chatId = deps.getActiveChatId();
    if (!chatId || !turn.prompt.trim() || isOptimisticQueuedTurn(turn)) return;
    deps.setComposeError(null);
    const cancelResult = await deps.api.pma.cancelQueuedTurn(chatId, turn.managedTurnId);
    if (!cancelResult.ok) {
      deps.setComposeError(cancelResult.error);
      return;
    }
    deps.readModelStore.setPmaQueue(
      chatId,
      selectQueue(chatId).filter((item) => item.managedTurnId !== turn.managedTurnId)
    );
    const clientTurnId = optimisticClientTurnId();
    deps.readModelStore.upsertChatTranscriptCards(chatId, [
      buildOptimisticUserTranscriptCard(chatId, turn.prompt, clientTurnId, isoNow())
    ]);
    const activeChat = deps.getActiveChat();
    const profileForSend = activeChat?.agentProfile?.trim() || deps.getSelectedProfile().trim() || '';
    const result = await executePmaChatCommandPlan(
      deps.api,
      planInterruptExistingChat(chatId, turn.prompt, {
        model: turn.model ?? deps.getSelectedModel(),
        attachments: turn.attachments as DocumentFileIntentPayload[],
        reasoning: turn.reasoning ?? deps.getSelectedReasoning(),
        profile: profileForSend,
        clientTurnId
      })
    );
    if (result.ok) {
      await deps.invalidateChatMutation(chatId);
      await deps.refreshActive(chatId, { quiet: true });
    } else {
      deps.readModelStore.removeOptimisticChatTranscriptCards(chatId);
      deps.setComposeError(result.error);
    }
  }

  async function clearQueue(options: { confirmed?: boolean } = {}): Promise<boolean> {
    const activeChatId = deps.getActiveChatId();
    if (!activeChatId) return false;
    const realTurns = selectQueue(activeChatId).filter((turn) => !isOptimisticQueuedTurn(turn));
    if (realTurns.length === 0) return false;
    if (!options.confirmed) {
      const ok = await deps.confirm({
        title: 'Clear queue',
        message: `Cancel all ${realTurns.length} queued message${realTurns.length === 1 ? '' : 's'}?`,
        confirmText: 'Clear queue',
        danger: true
      });
      if (!ok) return false;
    }
    deps.setComposeError(null);
    const result = await deps.api.pma.clearQueue(activeChatId);
    if (result.ok) {
      deps.readModelStore.setPmaQueue(activeChatId, []);
      await deps.refreshActive(activeChatId, { quiet: true });
      return true;
    } else {
      deps.setComposeError(result.error);
      return false;
    }
  }

  return {
    sendMessage,
    interruptWithDraft: (canInterrupt) => (canInterrupt ? sendMessage('interrupt') : Promise.resolve()),
    cancelQueuedTurn,
    interruptWithQueuedTurn,
    clearQueue
  };
}
