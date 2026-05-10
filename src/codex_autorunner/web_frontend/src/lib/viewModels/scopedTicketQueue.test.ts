import { describe, expect, it, vi } from 'vitest';
import type { TicketDetail } from './domain';
import {
  buildScopedTicketQueueCommandPlan,
  createScopedTicket,
  runScopedTicketQueueCommand,
  scopedTicketActionStatus,
  scopedTicketMissingRunStatus,
  scopedTicketQueueOwner,
  scopeToTicketOwner,
  scopeToTicketOwnerScope,
  scopeToTicketQueueConfig,
  ticketScopeHref,
  ticketScopeUrn,
  type ScopedTicketQueueConfig
} from './scopedTicketQueue';

const repoConfig: ScopedTicketQueueConfig = {
  kind: 'repo',
  resourceId: 'repo 1',
  apiBasePath: '/repos/repo%201/api/flows',
  displayLabel: 'repo'
};

const worktreeConfig: ScopedTicketQueueConfig = {
  kind: 'worktree',
  resourceId: 'wt-1',
  apiBasePath: '/repos/wt-1/api/flows',
  displayLabel: 'worktree',
  parentRepoId: null
};

describe('scoped ticket queue helpers', () => {
  it('returns ticket route id when create succeeds', async () => {
    const ticketFlow = {
      createTicket: vi.fn().mockResolvedValue({ ok: true, data: { id: 'T-1', number: 8 } as TicketDetail })
    };
    const result = await createScopedTicket({ ticketFlow } as unknown as Parameters<typeof createScopedTicket>[0], repoConfig, {
      title: 'A',
      body: 'B'
    });
    expect(result).toEqual({ ok: true, status: 'Ticket created.', ticketRouteId: '8' });
  });

  it('falls back to ticket id in route segment when create response has no index', async () => {
    const ticketFlow = {
      createTicket: vi.fn().mockResolvedValue({ ok: true, data: { id: 'TICKET-99', number: null } as TicketDetail })
    };
    const result = await createScopedTicket({ ticketFlow } as unknown as Parameters<typeof createScopedTicket>[0], repoConfig, {
      title: 'A',
      body: 'B'
    });
    expect(result).toEqual({ ok: true, status: 'Ticket created.', ticketRouteId: 'TICKET-99' });
  });

  it('surfaces API errors from create', async () => {
    const ticketFlow = {
      createTicket: vi.fn().mockResolvedValue({
        ok: false,
        error: { kind: 'http', status: 400, code: 'bad', message: 'Cannot create' }
      })
    };
    const result = await createScopedTicket({ ticketFlow } as unknown as Parameters<typeof createScopedTicket>[0], repoConfig, {
      title: 'A',
      body: 'B'
    });
    expect(result).toEqual({ ok: false, status: 'Cannot create' });
  });

  it('maps scope kinds to ticket API owners', () => {
    expect(scopedTicketQueueOwner(repoConfig)).toEqual({ repo: 'repo 1' });
    expect(scopedTicketQueueOwner(worktreeConfig)).toEqual({ worktree: 'wt-1' });
  });

  it('maps ScopeRef values to scoped ticket queue config and owner shapes', () => {
    const repoScope = { kind: 'repo' as const, id: 'repo 1' };
    const worktreeScope = { kind: 'worktree' as const, id: 'wt-1', parentRepoId: 'repo 1' };

    expect(scopeToTicketQueueConfig(repoScope)).toEqual({
      kind: 'repo',
      resourceId: 'repo 1',
      apiBasePath: '/repos/repo%201/api/flows',
      displayLabel: 'Repo: repo 1'
    });
    expect(scopeToTicketQueueConfig(worktreeScope)).toEqual({
      kind: 'worktree',
      resourceId: 'wt-1',
      apiBasePath: '/repos/wt-1/api/flows',
      displayLabel: 'Worktree: wt-1',
      parentRepoId: 'repo 1'
    });
    expect(scopeToTicketOwner(repoScope)).toEqual({ repo: 'repo 1' });
    expect(scopeToTicketOwner(worktreeScope)).toEqual({ worktree: 'wt-1' });
    expect(scopeToTicketOwnerScope(worktreeScope)).toEqual({ kind: 'worktree', id: 'wt-1', parentRepoId: 'repo 1' });
    expect(ticketScopeUrn(worktreeScope)).toBe('worktree:repo 1/wt-1');
    expect(ticketScopeHref(worktreeScope)).toBe('/repos/repo%201/worktrees/wt-1/tickets');
  });

  it('does not map non-ticket scopes into ticket queue owners', () => {
    expect(scopeToTicketQueueConfig({ kind: 'hub' })).toBeNull();
    expect(scopeToTicketOwner({ kind: 'filesystem', path: '/tmp/project' })).toBeNull();
    expect(ticketScopeHref({ kind: 'hub' })).toBeNull();
  });

  it('builds start, stop, and restart command requests', () => {
    expect(buildScopedTicketQueueCommandPlan('start', repoConfig, null)).toEqual({
      requests: [
        {
          path: '/repos/repo%201/api/flows/ticket_flow/bootstrap',
          options: { method: 'POST', body: {} }
        }
      ]
    });
    expect(buildScopedTicketQueueCommandPlan('stop', repoConfig, 'run/1')).toEqual({
      requests: [
        {
          path: '/repos/repo%201/api/flows/run%2F1/stop',
          options: { method: 'POST' }
        }
      ]
    });
    expect(buildScopedTicketQueueCommandPlan('restart', worktreeConfig, 'run-2')).toEqual({
      requests: [
        {
          path: '/repos/wt-1/api/flows/run-2/stop',
          options: { method: 'POST' }
        },
        {
          path: '/repos/wt-1/api/flows/ticket_flow/bootstrap',
          options: { method: 'POST', body: { metadata: { force_new: true } } }
        }
      ]
    });
  });

  it('returns null command plans when stop or restart has no run id', () => {
    expect(buildScopedTicketQueueCommandPlan('stop', repoConfig, null)).toBeNull();
    expect(buildScopedTicketQueueCommandPlan('restart', repoConfig, null)).toBeNull();
  });

  it('formats action status by scope label', () => {
    expect(scopedTicketActionStatus('create', repoConfig)).toBe('Creating repo ticket...');
    expect(scopedTicketActionStatus('reorder', worktreeConfig)).toBe('Reordering worktree tickets...');
    expect(scopedTicketActionStatus('start', repoConfig)).toBe('Starting repo ticket flow...');
    expect(scopedTicketActionStatus('stop', worktreeConfig)).toBe('Stopping worktree ticket flow...');
    expect(scopedTicketActionStatus('restart', worktreeConfig)).toBe('Restarting worktree ticket flow...');
    expect(scopedTicketMissingRunStatus(repoConfig)).toBe('No repo ticket flow run found.');
  });

  it('executes restart as stop then force-new bootstrap', async () => {
    const requestJson = vi.fn().mockResolvedValue({ ok: true, data: {} });
    const result = await runScopedTicketQueueCommand(
      { requestJson },
      worktreeConfig,
      'restart',
      'run-2',
      () => true
    );

    expect(result).toEqual({ status: 'Ticket flow command accepted.', shouldReload: true });
    expect(requestJson).toHaveBeenNthCalledWith(1, '/repos/wt-1/api/flows/run-2/stop', { method: 'POST' });
    expect(requestJson).toHaveBeenNthCalledWith(2, '/repos/wt-1/api/flows/ticket_flow/bootstrap', {
      method: 'POST',
      body: { metadata: { force_new: true } }
    });
  });

  it('executes backend manifest routes without a separately loaded run id', async () => {
    const requestJson = vi.fn().mockResolvedValue({ ok: true, data: {} });
    const result = await runScopedTicketQueueCommand(
      { requestJson },
      repoConfig,
      'stop',
      null,
      () => true,
      {
        action: 'stop',
        enabled: true,
        label: 'Stop',
        requiresConfirmation: false,
        disabledReason: null,
        method: 'POST',
        route: '/repos/repo%201/api/flows/run-from-manifest/stop'
      }
    );

    expect(result).toEqual({ status: 'Ticket flow command accepted.', shouldReload: true });
    expect(requestJson).toHaveBeenCalledWith('/repos/repo%201/api/flows/run-from-manifest/stop', {
      method: 'POST'
    });
  });

  it('does not execute restart when confirmation is declined', async () => {
    const requestJson = vi.fn().mockResolvedValue({ ok: true, data: {} });
    const result = await runScopedTicketQueueCommand(
      { requestJson },
      repoConfig,
      'restart',
      'run-1',
      () => false
    );

    expect(result).toEqual({ status: null, shouldReload: false });
    expect(requestJson).not.toHaveBeenCalled();
  });
});
