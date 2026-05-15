import { describe, expect, it, vi } from 'vitest';
import { WebApiClient } from '$lib/api/client';
import { localPmaChatScopeOption } from '$lib/viewModels/pmaChat';
import {
  executePmaChatCommandPlan,
  planForkChat,
  planInterruptExistingChat,
  planQueueExistingChat,
  planSendExistingChat,
  planStartAndSendChat,
  planStartChat
} from './pmaChatCommands';

describe('PMA chat command plans', () => {
  it('plans start chat as the only command that creates a thread', () => {
    expect(planStartChat(localPmaChatScopeOption(), 'hermes', 'planning', 'gpt-5.2')).toEqual({
      kind: 'StartChat',
      body: {
        agent: 'hermes',
        chat_kind: 'pma',
        name: 'New chat',
        profile: 'planning',
        model: 'gpt-5.2',
        scope_urn: 'hub'
      }
    });
    expect(planStartChat(localPmaChatScopeOption(), 'codex', '', '', 'New coding agent chat', 'coding_agent')).toMatchObject({
      body: {
        agent: 'codex',
        chat_kind: 'coding_agent',
        name: 'New coding agent chat',
        scope_urn: 'hub'
      }
    });
  });

  it('plans existing sends against only the supplied thread id', () => {
    expect(
      planSendExistingChat('thread-1', 'Continue', {
        model: 'gpt-5.2',
        isRunning: false,
        profile: '   '
      })
    ).toEqual({
      kind: 'SendMessage',
      threadId: 'thread-1',
      body: {
        message: 'Continue',
        attachments: undefined,
        model: 'gpt-5.2',
        reasoning: undefined,
        busy_policy: undefined,
        defer_execution: true,
        wait_for_confirmation: false
      }
    });
  });

  it('plans draft first sends as a single start-and-send command', () => {
    expect(
      planStartAndSendChat(localPmaChatScopeOption(), 'hermes', 'planning', '', 'Hello', {
        reasoning: 'high',
        clientTurnId: 'client-1'
      })
    ).toMatchObject({
      kind: 'StartAndSendChat',
      body: {
        agent: 'hermes',
        profile: 'planning',
        message: 'Hello',
        reasoning: 'high',
        client_turn_id: 'client-1',
        scope_urn: 'hub',
        wait_for_confirmation: false
      }
    });
  });

  it('plans queue and interrupt policies explicitly for existing threads', () => {
    expect(planQueueExistingChat('thread-1', 'Queue this')).toMatchObject({
      kind: 'SendMessage',
      threadId: 'thread-1',
      body: { busy_policy: 'queue' }
    });
    expect(planInterruptExistingChat('thread-1', 'Replace this')).toMatchObject({
      kind: 'SendMessage',
      threadId: 'thread-1',
      body: { busy_policy: 'interrupt' }
    });
  });

  it('rejects existing-thread commands without a durable thread id', () => {
    expect(() => planSendExistingChat('', 'Continue')).toThrow('thread id');
    expect(() => planForkChat('   ')).toThrow('thread id');
  });
});

describe('PMA chat command execution', () => {
  it('sends existing-chat follow-ups only to the messages endpoint', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/pma/threads/thread-1/messages') {
        return Response.json({ message: { id: 'msg-1', role: 'user', text: 'Continue' } });
      }
      return Response.json({ thread: { thread_target_id: 'unexpected' } });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await executePmaChatCommandPlan(client, planSendExistingChat('thread-1', 'Continue'));

    expect(result.ok).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/threads/thread-1/messages',
      expect.objectContaining({ method: 'POST' })
    );
    expect(fetcher).not.toHaveBeenCalledWith('/hub/pma/threads', expect.any(Object));
  });

  it('executes start and fork through their explicit endpoints', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({ thread: { thread_target_id: 'thread-new', display_name: 'New chat' } })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await executePmaChatCommandPlan(client, planStartChat(localPmaChatScopeOption(), 'codex'));
    await executePmaChatCommandPlan(client, planForkChat('thread-1', { name: 'Forked chat' }));

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/threads/thread-1/fork',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'Forked chat' })
      })
    );
  });

  it('executes draft start-and-send through the composite endpoint', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({ managed_thread_id: 'thread-new', managed_turn_id: 'turn-1', delivered_message: 'Hello' })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await executePmaChatCommandPlan(
      client,
      planStartAndSendChat(localPmaChatScopeOption(), 'hermes', '', '', 'Hello')
    );

    expect(result.ok).toBe(true);
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/thread-starts',
      expect.objectContaining({ method: 'POST' })
    );
  });
});
