import type { ApiResult, PmaApiClient } from '$lib/api/client';
import type { PmaChatMessage, PmaChatSummary } from '$lib/viewModels/domain';
import {
  buildManagedThreadCreatePayload,
  buildManagedThreadStartMessagePayload,
  buildManagedThreadMessagePayload,
  type DocumentFileIntentPayload,
  type ManagedThreadCreatePayload,
  type ManagedThreadMessagePayload,
  type ManagedThreadStartMessagePayload,
  type PendingAttachment,
  type PmaChatScopeOption
} from '$lib/viewModels/pmaChat';
import type { PmaChatKind } from '$lib/viewModels/pmaChat';

export type PmaChatBusyPolicy = 'queue' | 'interrupt' | 'reject';

export type StartPmaChatPlan = {
  kind: 'StartChat';
  body: ManagedThreadCreatePayload;
};

export type SendExistingPmaChatPlan = {
  kind: 'SendMessage';
  threadId: string;
  body: ManagedThreadMessagePayload;
};

export type StartAndSendPmaChatPlan = {
  kind: 'StartAndSendChat';
  body: ManagedThreadStartMessagePayload;
};

export type ForkPmaChatPlan = {
  kind: 'ForkChat';
  threadId: string;
  body: PmaChatForkPayload;
};

export type PmaChatCommandPlan = StartPmaChatPlan | StartAndSendPmaChatPlan | SendExistingPmaChatPlan | ForkPmaChatPlan;

export type PmaChatForkPayload = {
  name?: string;
};

export type ExistingPmaChatMessageOptions = {
  model?: string;
  isRunning?: boolean;
  attachments?: Array<PendingAttachment | DocumentFileIntentPayload>;
  reasoning?: string;
  profile?: string;
  busyPolicy?: PmaChatBusyPolicy | null;
  clientTurnId?: string;
};

type PmaCommandClient = {
  pma: Pick<PmaApiClient['pma'], 'createChat' | 'startChatWithMessage' | 'sendMessage' | 'forkThread'>;
};

function requireThreadId(threadId: string): string {
  const trimmed = threadId.trim();
  if (!trimmed) throw new Error('Existing PMA chat commands require a thread id.');
  return trimmed;
}

export function planStartChat(
  scope: PmaChatScopeOption,
  agent: string,
  profile = '',
  model = '',
  name = 'New chat',
  chatKind: PmaChatKind = 'pma'
): StartPmaChatPlan {
  return {
    kind: 'StartChat',
    body: buildManagedThreadCreatePayload(agent, scope, name, model, profile, chatKind)
  };
}

export function planStartAndSendChat(
  scope: PmaChatScopeOption,
  agent: string,
  profile: string,
  model: string,
  message: string,
  options: {
    name?: string;
    chatKind?: PmaChatKind;
    attachments?: Array<PendingAttachment | DocumentFileIntentPayload>;
    reasoning?: string;
    clientTurnId?: string;
  } = {}
): StartAndSendPmaChatPlan {
  return {
    kind: 'StartAndSendChat',
    body: buildManagedThreadStartMessagePayload(
      scope,
      agent,
      profile,
      model,
      options.name ?? 'New chat',
      options.chatKind ?? 'pma',
      message,
      options.attachments ?? [],
      options.reasoning ?? '',
      options.clientTurnId ?? ''
    )
  };
}

export function planSendExistingChat(
  threadId: string,
  message: string,
  options: ExistingPmaChatMessageOptions = {}
): SendExistingPmaChatPlan {
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
  options: Omit<ExistingPmaChatMessageOptions, 'busyPolicy'> = {}
): SendExistingPmaChatPlan {
  return planSendExistingChat(threadId, message, { ...options, isRunning: true, busyPolicy: 'queue' });
}

export function planInterruptExistingChat(
  threadId: string,
  message: string,
  options: Omit<ExistingPmaChatMessageOptions, 'busyPolicy'> = {}
): SendExistingPmaChatPlan {
  return planSendExistingChat(threadId, message, { ...options, isRunning: true, busyPolicy: 'interrupt' });
}

export function planForkChat(threadId: string, overrides: PmaChatForkPayload = {}): ForkPmaChatPlan {
  return {
    kind: 'ForkChat',
    threadId: requireThreadId(threadId),
    body: { ...overrides }
  };
}

export async function executePmaChatCommandPlan(
  client: PmaCommandClient,
  plan: StartPmaChatPlan | ForkPmaChatPlan
): Promise<ApiResult<PmaChatSummary>>;
export async function executePmaChatCommandPlan(
  client: PmaCommandClient,
  plan: StartAndSendPmaChatPlan | SendExistingPmaChatPlan
): Promise<ApiResult<PmaChatMessage>>;
export async function executePmaChatCommandPlan(
  client: PmaCommandClient,
  plan: PmaChatCommandPlan
): Promise<ApiResult<PmaChatSummary> | ApiResult<PmaChatMessage>> {
  if (plan.kind === 'StartChat') return client.pma.createChat(plan.body);
  if (plan.kind === 'StartAndSendChat') return client.pma.startChatWithMessage(plan.body);
  if (plan.kind === 'ForkChat') return client.pma.forkThread(plan.threadId, plan.body);
  return client.pma.sendMessage(plan.threadId, plan.body);
}
