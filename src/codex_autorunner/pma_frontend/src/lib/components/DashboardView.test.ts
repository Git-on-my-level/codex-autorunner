import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import DashboardView from './DashboardView.svelte';
import { buildDashboardViewModel } from '$lib/viewModels/dashboard';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketSummary } from '$lib/viewModels/mockData';

describe('DashboardView', () => {
  afterEach(() => {
    globalThis.__CAR_BASE_PATH__ = undefined;
  });

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
      tickets: []
    });
    const { body } = render(DashboardView, { props: { state: 'ready', dashboard } });

    expect(body).toContain('No active CAR work');
    expect(body).not.toContain('Open PMA');
    expect(body).not.toContain('No active runs');
    expect(body).not.toContain('Start or resume a ticket-flow queue to see active work here.');
    expect(body).toContain('Ticket-flow queues');
    expect(body).toContain('No queues yet');
    expect(body).not.toContain('View repos');
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
      tickets: [mockTicketSummary]
    });
    const { body } = render(DashboardView, { props: { state: 'ready', dashboard } });

    expect(body).toContain('Active runs');
    expect(body).toContain('Per-repo and per-worktree ticket-flow state. Active runs appear first.');
    expect(body).not.toContain('Live ticket-flow runs. Ticket-flow queues appear below.');
    expect(body).toContain('Hub rewrite foundation');
    expect(body).toContain('href="/worktrees/worktree-1/tickets/110"');
    expect(body).not.toContain('Repos and worktree variants');
    expect(body).not.toContain('Repo worktrees');
    expect(body).toContain('Recent activity');
    expect(body).toContain('Preview ready');
  });

  it('renders a degraded secondary section with a retry affordance', () => {
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [mockRunProgress],
      chats: [],
      tickets: []
    });
    const { body } = render(DashboardView, {
      props: {
        state: 'ready',
        dashboard,
        sectionIssues: [
          {
            id: 'waiting_for_me',
            title: 'Attention queue unavailable',
            message: 'automation endpoint returned 503',
            retryLabel: 'Retry'
          }
        ],
        onRetry: () => {}
      }
    });

    expect(body).toContain('Attention queue unavailable');
    expect(body).toContain('automation endpoint returned 503');
    expect(body).toContain('<button type="button">Retry</button>');
    expect(body).toContain('Ticket-flow queues');
  });

  it('renders internal links under the injected hub base path', () => {
    globalThis.__CAR_BASE_PATH__ = '/car';
    const dashboard = buildDashboardViewModel({
      summary: null,
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      tickets: [mockTicketSummary]
    });
    const { body } = render(DashboardView, { props: { state: 'ready', dashboard } });

    expect(body).toContain('href="/car/worktrees/worktree-1/tickets/110"');
  });
});
