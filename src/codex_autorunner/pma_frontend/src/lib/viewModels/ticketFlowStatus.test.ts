import { describe, expect, it } from 'vitest';
import { mockTicketSummary } from './mockData';
import { buildTicketFlowStatusViewModel } from './ticketFlowStatus';

describe('ticket flow status routes', () => {
  it('links current worktree tickets through the parent repo when available', () => {
    const vm = buildTicketFlowStatusViewModel(
      [
        {
          ...mockTicketSummary,
          number: 27,
          workspaceKind: 'worktree',
          workspaceId: 'discord-5',
          repoId: 'codex-autorunner',
          worktreeId: 'discord-5'
        }
      ],
      [],
      { kind: 'worktree', id: 'discord-5' }
    );

    expect(vm.currentTicketHref).toBe('/repos/codex-autorunner/worktrees/discord-5/tickets/27');
  });

  it('keeps parentless worktree ticket links as compatibility fallbacks', () => {
    const vm = buildTicketFlowStatusViewModel(
      [
        {
          ...mockTicketSummary,
          number: 27,
          workspaceKind: 'worktree',
          workspaceId: 'orphan',
          repoId: null,
          worktreeId: 'orphan'
        }
      ],
      [],
      { kind: 'worktree', id: 'orphan' }
    );

    expect(vm.currentTicketHref).toBe('/worktrees/orphan/tickets/27');
  });

  it('does not treat pending stop-requested runs as active work', () => {
    const vm = buildTicketFlowStatusViewModel(
      [],
      [
        {
          id: 'run-stale',
          chatId: null,
          status: 'waiting',
          workStatus: null,
          operatorStatus: null,
          terminal: false,
          streamShouldClose: false,
          streamCloseReason: null,
          phase: null,
          guidance: 'Stop requested',
          queueDepth: 0,
          elapsedSeconds: null,
          idleSeconds: null,
          lastEventId: null,
          lastEventAt: '2026-05-04T00:02:00Z',
          progressPercent: null,
          events: [],
          raw: {
            id: 'run-stale',
            status: 'pending',
            stop_requested: true,
            repo_id: 'codex-autorunner'
          }
        }
      ],
      { kind: 'repo', id: 'codex-autorunner' }
    );

    expect(vm.status).toBe('idle');
    expect(vm.signal).toBe('idle');
  });
});
