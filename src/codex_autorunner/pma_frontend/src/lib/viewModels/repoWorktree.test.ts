import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from './mockData';
import { buildRepoWorktreeDetailViewModel, buildRepoWorktreeIndexViewModel } from './repoWorktree';

describe('repo/worktree view models', () => {
  it('builds a lightweight repo index with child worktrees grouped under the repo', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
      chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [mockTicketSummary],
      artifacts: []
    });

    expect(vm.rows).toHaveLength(1);
    expect(vm.title).toBe('Repos');
    expect(vm.eyebrow).toBe('Repo ownership');
    expect(vm.activeCount).toBe(1);
    expect(vm.openTicketCount).toBe(4);
    expect(vm.rows[0]).toMatchObject({
      href: '/repos/repo-1',
      childWorktrees: [
        {
          href: '/worktrees/worktree-1',
          currentTicketId: 'TICKET-110',
          currentRunTitle: 'Hub rewrite foundation'
        }
      ]
    });
  });

  it('frames the worktree index as repo-owned variants', () => {
    const vm = buildRepoWorktreeIndexViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [],
        chats: [],
        tickets: [],
        artifacts: []
      },
      'worktree'
    );

    expect(vm.title).toBe('Secondary worktree index');
    expect(vm.eyebrow).toBe('Repo-owned variants');
    expect(vm.rows[0]).toMatchObject({
      href: '/worktrees/worktree-1',
      repoHref: '/repos/repo-1'
    });
    expect(vm.rows[0].detail).toContain('Repo worktree variant');
  });

  it('builds active current-run detail with links and artifacts', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1' }],
        tickets: [mockTicketSummary],
        artifacts: [mockArtifact]
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(true);
    expect(vm.currentRuns[0]).toMatchObject({
      title: 'Hub rewrite foundation',
      agentId: 'codex',
      ticketHref: '/tickets/TICKET-110'
    });
    expect(vm.links.map((link) => link.label)).toContain('Open PMA chat');
    expect(vm.links.map((link) => link.label)).toContain('View workspace tickets');
    expect(vm.links.map((link) => link.label)).toContain('View workspace memory');
    expect(vm.links.map((link) => link.label)).toContain('Open preview');
    expect(vm.artifacts[0]).toMatchObject({ kind: 'preview_url' });
    expect(vm.childWorktrees).toHaveLength(1);
    expect(vm.childWorktrees[0]).toMatchObject({
      href: '/worktrees/worktree-1',
      currentTicketId: 'TICKET-110'
    });
  });

  it('names the base repo on worktree detail when known', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: 'worktree-1' }],
        tickets: [mockTicketSummary],
        artifacts: []
      },
      'worktree',
      'worktree-1'
    );

    expect(vm.baseRepoLabel).toBe('codex-autorunner');
    expect(vm.baseRepoHref).toBe('/repos/repo-1');
    expect(vm.currentRuns[0].ticketHref).toBe('/tickets/TICKET-110');
  });

  it('does not match repo-level records on worktree detail through the parent repo id', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: null }],
        tickets: [{ ...mockTicketSummary, worktreeId: null, raw: { repo_id: 'repo-1' } }],
        artifacts: []
      },
      'worktree',
      'worktree-1'
    );

    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets).toHaveLength(0);
  });

  it('builds no-active-run detail without promoting debug as primary', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [{ ...mockTicketSummary, status: 'idle' }],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(false);
    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets[0].title).toBe(mockTicketSummary.title);
    expect(vm.links.find((link) => link.label === 'Ticket diagnostics')?.secondary).toBe(true);
  });

  it('scopes queued tickets to the selected repo when ticket ownership is known', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, id: 'repo-1', status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [
          { ...mockTicketSummary, id: 'ticket-a', title: 'Repo ticket', repoId: 'repo-1' },
          { ...mockTicketSummary, id: 'ticket-b', title: 'Other repo ticket', repoId: 'repo-2' }
        ],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.nextTickets.map((ticket) => ticket.title)).toEqual(['Repo ticket']);
  });
});
