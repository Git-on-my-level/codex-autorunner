import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketSummary } from './mockData';
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
        raw: {
          action_queue: [
            {
              action_queue_id: 'managed_thread_followup:thread-1',
              queue_source: 'managed_thread_followup',
              item_type: 'managed_thread_followup',
              name: 'Managed thread follow-up',
              recommended_detail: 'Managed thread needs a response.',
              managed_thread_id: 'thread-1',
              freshness: { basis_at: '2026-05-04T00:04:00Z' }
            }
          ]
        }
      },
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      tickets: [
        mockTicketSummary,
        { ...mockTicketSummary, id: 'TICKET-111', number: 111, status: 'blocked', title: 'Blocked ticket' }
      ]
    });

    expect(dashboard.metrics.map((metric) => [metric.label, metric.value])).toEqual([
      ['Active runs', 1],
      ['Waiting for me', 1],
      ['Failed/blocked', 1],
      ['Open tickets', 2]
    ]);
    expect(dashboard.metrics.find((metric) => metric.label === 'Active runs')?.href).toBe('#queues');
    expect(dashboard.activeRuns[0]).toMatchObject({
      title: 'Hub rewrite foundation',
      kindLabel: 'Ticket flow',
      primaryHref: '/pma?chat=chat-1',
      ticketHref: '/repos/codex-autorunner/tickets/TICKET-110',
      repoHref: '/repos/codex-autorunner'
    });
    expect(dashboard.waitingForMe[0]).toMatchObject({
      title: 'Managed thread follow-up',
      kind: 'followup',
      primaryHref: '/pma?chat=thread-1'
    });
    expect(dashboard.failedOrBlocked[0]).toMatchObject({
      title: 'Blocked ticket',
      primaryHref: '/worktrees/worktree-1/tickets/111'
    });
    expect(dashboard.recentActivity.some((activity) => activity.title === 'Preview ready')).toBe(true);
  });

  it('does not promote active PMA chats into active ticket-flow runs', () => {
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [],
      chats: [mockChatSummary],
      tickets: []
    });

    expect(dashboard.activeRuns).toEqual([]);
    expect(dashboard.metrics.find((metric) => metric.label === 'Active runs')?.value).toBe(0);
    expect(dashboard.hasAnyData).toBe(true);
  });

  it('keeps an explicit empty model useful', () => {
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [],
      chats: [],
      tickets: []
    });

    expect(dashboard.hasAnyData).toBe(false);
    expect(dashboard.metrics.every((metric) => metric.value === 0)).toBe(true);
    expect(dashboard.recentActivity).toEqual([]);
  });
});
