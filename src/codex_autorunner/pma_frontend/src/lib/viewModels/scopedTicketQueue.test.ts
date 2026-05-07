import { describe, expect, it, vi } from 'vitest';
import {
  buildScopedTicketQueueCommandPlan,
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
  displayLabel: 'worktree'
};

describe('scoped ticket queue helpers', () => {
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
    expect(scopeToTicketOwner(repoScope)).toEqual({ repo: 'repo 1' });
    expect(scopeToTicketOwner(worktreeScope)).toEqual({ worktree: 'wt-1' });
    expect(scopeToTicketOwnerScope(worktreeScope)).toEqual({ kind: 'worktree', id: 'wt-1' });
    expect(ticketScopeUrn(worktreeScope)).toBe('worktree:repo 1/wt-1');
    expect(ticketScopeHref(worktreeScope)).toBe('/worktrees/wt-1/tickets');
  });

  it('does not map non-ticket scopes into ticket queue owners', () => {
    expect(scopeToTicketQueueConfig({ kind: 'hub' })).toBeNull();
    expect(scopeToTicketOwner({ kind: 'filesystem', path: '/tmp/project' })).toBeNull();
    expect(scopeToTicketOwnerScope({ kind: 'agent_workspace', id: 'codex' })).toBeNull();
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
