import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from './mockData';
import { buildDashboardViewModel } from './dashboard';

describe('dashboard view model', () => {
  it('builds summary cards and linked operational sections', () => {
    const dashboard = buildDashboardViewModel({
      summary: {
        activeRuns: 1,
        waitingForUser: 1,
        failedOrBlocked: 1,
        openTickets: 2,
        repos: 1,
        worktrees: 1,
        recentArtifacts: [mockArtifact],
        raw: {}
      },
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      approvals: [
        {
          id: 'approval-1',
          title: 'Approve cleanup',
          description: 'Sensitive cleanup request.',
          risk: 'high',
          action: 'cleanup',
          createdAt: '2026-05-04T00:04:00Z',
          raw: {}
        }
      ],
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      tickets: [
        mockTicketSummary,
        { ...mockTicketSummary, id: 'TICKET-111', status: 'blocked', title: 'Blocked ticket' }
      ]
    });

    expect(dashboard.metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ['Active runs', 1],
      ['Waiting for me', 1],
      ['Failed/blocked', 1],
      ['Open tickets', 2],
      ['Repos', 1],
      ['Worktrees', 1]
    ]);
    expect(dashboard.activeRuns[0]).toMatchObject({
      title: 'Hub rewrite foundation',
      primaryHref: '/pma?chat=chat-1',
      ticketHref: '/tickets/TICKET-110',
      repoHref: '/repos/codex-autorunner'
    });
    expect(dashboard.waitingForMe[0]).toMatchObject({
      title: 'Approve cleanup',
      primaryHref: '/settings'
    });
    expect(dashboard.failedOrBlocked[0]).toMatchObject({
      title: 'Blocked ticket',
      primaryHref: '/tickets/TICKET-111'
    });
    expect(dashboard.repoWorktrees).toHaveLength(2);
    expect(dashboard.recentActivity.some((activity) => activity.title === 'Preview ready')).toBe(true);
  });

  it('keeps an explicit empty model useful', () => {
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [],
      chats: [],
      approvals: [],
      repos: [],
      worktrees: [],
      tickets: []
    });

    expect(dashboard.hasAnyData).toBe(false);
    expect(dashboard.metrics.every((metric) => metric.value === 0)).toBe(true);
    expect(dashboard.recentActivity).toEqual([]);
  });
});
