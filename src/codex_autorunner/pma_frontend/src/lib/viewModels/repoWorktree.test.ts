import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from './mockData';
import { buildRepoWorktreeDetailViewModel, buildRepoWorktreeIndexViewModel } from './repoWorktree';

describe('repo/worktree view models', () => {
  it('builds a lightweight mixed repo/worktree index', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      tickets: [mockTicketSummary],
      artifacts: []
    });

    expect(vm.rows).toHaveLength(2);
    expect(vm.activeCount).toBe(2);
    expect(vm.openTicketCount).toBe(4);
    expect(vm.rows[0]).toHaveProperty('href');
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
    expect(vm.links.map((link) => link.label)).toContain('Open preview');
    expect(vm.artifacts[0]).toMatchObject({ kind: 'preview_url' });
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
    expect(vm.links.find((link) => link.label.includes('Debug'))?.secondary).toBe(true);
  });
});
