import type { ApiResult, WebApiClient } from '$lib/api/client';
import type { ChatMessage, ChatSummary } from '$lib/viewModels/domain';
import {
  buildManagedThreadCreatePayload,
  buildManagedThreadStartMessagePayload,
  buildManagedThreadMessagePayload,
  type DocumentFileIntentPayload,
  type ManagedThreadCreatePayload,
  type ManagedThreadMessagePayload,
  type ManagedThreadStartMessagePayload,
  type PendingAttachment,
  type ChatScopeOption,
  type ChatScopeSource
} from '$lib/viewModels/chat';
import type { ChatKind } from '$lib/viewModels/chat';

export type ChatBusyPolicy = 'queue' | 'interrupt' | 'reject';

export type StartChatPlan = {
  kind: 'StartChat';
  body: ManagedThreadCreatePayload;
};

export type SendExistingChatPlan = {
  kind: 'SendMessage';
  threadId: string;
  body: ManagedThreadMessagePayload;
};

export type StartAndSendChatPlan = {
  kind: 'StartAndSendChat';
  body: ManagedThreadStartMessagePayload;
};

export type ForkChatPlan = {
  kind: 'ForkChat';
  threadId: string;
  body: ChatForkPayload;
};

export type ChatCommandPlan = StartChatPlan | StartAndSendChatPlan | SendExistingChatPlan | ForkChatPlan;

export type ChatForkPayload = {
  name?: string;
};

export type ExistingChatMessageOptions = {
  model?: string;
  isRunning?: boolean;
  attachments?: Array<PendingAttachment | DocumentFileIntentPayload>;
  reasoning?: string;
  profile?: string;
  busyPolicy?: ChatBusyPolicy | null;
  clientTurnId?: string;
};

type ChatCommandClient = {
  pma: Pick<WebApiClient['pma'], 'createChat' | 'startChatWithMessage' | 'sendMessage' | 'forkThread'>;
};

function requireThreadId(threadId: string): string {
  const trimmed = threadId.trim();
  if (!trimmed) throw new Error('Existing chat commands require a thread id.');
  return trimmed;
}

export function planStartChat(
  scope: ChatScopeOption,
  agent: string,
  profile = '',
  model = '',
  name = 'New chat',
  chatKind: ChatKind = 'pma'
): StartChatPlan {
  return {
    kind: 'StartChat',
    body: buildManagedThreadCreatePayload(agent, scope, name, model, profile, chatKind)
  };
}

export function planStartAndSendChat(
  chatId: string,
  scope: ChatScopeOption,
  agent: string,
  profile: string,
  model: string,
  message: string,
  options: {
    name?: string;
    chatKind?: ChatKind;
    attachments?: Array<PendingAttachment | DocumentFileIntentPayload>;
    reasoning?: string;
    clientTurnId?: string;
    scopeSource?: ChatScopeSource;
  } = {}
): StartAndSendChatPlan {
  return {
    kind: 'StartAndSendChat',
    body: buildManagedThreadStartMessagePayload(
      chatId,
      scope,
      agent,
      profile,
      model,
      options.name ?? 'New chat',
      options.chatKind ?? 'pma',
      message,
      options.attachments ?? [],
      options.reasoning ?? '',
      options.clientTurnId ?? '',
      options.scopeSource ?? 'default_hub'
    )
  };
}

export function planSendExistingChat(
  threadId: string,
  message: string,
  options: ExistingChatMessageOptions = {}
): SendExistingChatPlan {
  const isRunning = options.isRunning ?? false;
  return {
    kind: 'SendMessage',
    threadId: requireThreadId(threadId),
    body: buildManagedThreadMessagePayload(
      message,
      options.model ?? '',
      isRunning,
      options.attachments ?? [],
      options.reasoning ?? '',
      options.profile ?? '',
      options.busyPolicy ?? (isRunning ? 'queue' : null),
      options.clientTurnId ?? ''
    )
  };
}

export function planQueueExistingChat(
  threadId: string,
  message: string,
  options: Omit<ExistingChatMessageOptions, 'busyPolicy'> = {}
): SendExistingChatPlan {
  return planSendExistingChat(threadId, message, { ...options, isRunning: true, busyPolicy: 'queue' });
}

export function planInterruptExistingChat(
  threadId: string,
  message: string,
  options: Omit<ExistingChatMessageOptions, 'busyPolicy'> = {}
): SendExistingChatPlan {
  return planSendExistingChat(threadId, message, { ...options, isRunning: true, busyPolicy: 'interrupt' });
}

export function planForkChat(threadId: string, overrides: ChatForkPayload = {}): ForkChatPlan {
  return {
    kind: 'ForkChat',
    threadId: requireThreadId(threadId),
    body: { ...overrides }
  };
}

export async function executeChatCommandPlan(
  client: ChatCommandClient,
  plan: StartChatPlan | ForkChatPlan
): Promise<ApiResult<ChatSummary>>;
export async function executeChatCommandPlan(
  client: ChatCommandClient,
  plan: StartAndSendChatPlan | SendExistingChatPlan
): Promise<ApiResult<ChatMessage>>;
export async function executeChatCommandPlan(
  client: ChatCommandClient,
  plan: ChatCommandPlan
): Promise<ApiResult<ChatSummary> | ApiResult<ChatMessage>> {
  if (plan.kind === 'StartChat') return client.pma.createChat(plan.body);
  if (plan.kind === 'StartAndSendChat') return client.pma.startChatWithMessage(plan.body);
  if (plan.kind === 'ForkChat') return client.pma.forkThread(plan.threadId, plan.body);
  return client.pma.sendMessage(plan.threadId, plan.body);
}
