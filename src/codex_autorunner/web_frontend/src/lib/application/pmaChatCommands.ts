import type { ApiResult, PmaApiClient } from '$lib/api/client';
import type { PmaChatMessage, PmaChatSummary } from '$lib/viewModels/domain';
import {
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  type DocumentFileIntentPayload,
  type ManagedThreadCreatePayload,
  type ManagedThreadMessagePayload,
  type PendingAttachment,
  type PmaChatScopeOption
} from '$lib/viewModels/pmaChat';

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

export type ForkPmaChatPlan = {
  kind: 'ForkChat';
  threadId: string;
  body: PmaChatForkPayload;
};

export type PmaChatCommandPlan = StartPmaChatPlan | SendExistingPmaChatPlan | ForkPmaChatPlan;

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
};

type PmaCommandClient = {
  pma: Pick<PmaApiClient['pma'], 'createChat' | 'sendMessage' | 'forkThread'>;
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
  chatKind: 'pma' | 'coding_agent' = 'pma'
): StartPmaChatPlan {
  return {
    kind: 'StartChat',
    body: buildManagedThreadCreatePayload(agent, scope, name, model, profile, chatKind)
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
      options.busyPolicy ?? (isRunning ? 'queue' : null)
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
  plan: SendExistingPmaChatPlan
): Promise<ApiResult<PmaChatMessage>>;
export async function executePmaChatCommandPlan(
  client: PmaCommandClient,
  plan: PmaChatCommandPlan
): Promise<ApiResult<PmaChatSummary> | ApiResult<PmaChatMessage>> {
  if (plan.kind === 'StartChat') return client.pma.createChat(plan.body);
  if (plan.kind === 'ForkChat') return client.pma.forkThread(plan.threadId, plan.body);
  return client.pma.sendMessage(plan.threadId, plan.body);
}
