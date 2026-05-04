import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import DashboardView from './DashboardView.svelte';
import { buildDashboardViewModel } from '$lib/viewModels/dashboard';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from '$lib/viewModels/mockData';

describe('DashboardView', () => {
  it('renders loading state', () => {
    const { body } = render(DashboardView, { props: { state: 'loading' } });

    expect(body).toContain('Loading dashboard...');
  });

  it('renders error state', () => {
    const { body } = render(DashboardView, {
      props: { state: 'error', errorMessage: 'Network unavailable.' }
    });

    expect(body).toContain('Could not load dashboard. Network unavailable.');
  });

  it('renders useful empty state', () => {
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [],
      chats: [],
      approvals: [],
      repos: [],
      worktrees: [],
      tickets: []
    });
    const { body } = render(DashboardView, { props: { state: 'ready', dashboard } });

    expect(body).toContain('No active CAR work');
    expect(body).toContain('Open PMA');
    expect(body).toContain('View repos');
    expect(body).toContain('No runs are currently active.');
  });

  it('renders populated dashboard sections and links', () => {
    const dashboard = buildDashboardViewModel({
      summary: {
        activeRuns: 1,
        waitingForUser: 0,
        failedOrBlocked: 0,
        openTickets: 1,
        repos: 1,
        worktrees: 1,
        recentArtifacts: [mockArtifact],
        raw: {}
      },
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      approvals: [],
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      tickets: [mockTicketSummary]
    });
    const { body } = render(DashboardView, { props: { state: 'ready', dashboard } });

    expect(body).toContain('Active runs');
    expect(body).toContain('Hub rewrite foundation');
    expect(body).toContain('href="/pma?chat=chat-1"');
    expect(body).toContain('href="/repos/codex-autorunner"');
    expect(body).toContain('href="/tickets/TICKET-110"');
    expect(body).toContain('Repos and worktrees');
    expect(body).toContain('Recent activity');
    expect(body).toContain('Preview ready');
  });
});
