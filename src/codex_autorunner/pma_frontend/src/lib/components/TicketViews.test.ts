import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import TicketViews from './TicketViews.svelte';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketDetail, mockTicketSummary } from '$lib/viewModels/mockData';
import { buildTicketDetailViewModel, buildTicketListViewModel } from '$lib/viewModels/ticket';

describe('TicketViews', () => {
  it('renders ticket rows without exposing raw markdown as the only representation', () => {
    const list = buildTicketListViewModel({
      tickets: [mockTicketSummary],
      runs: [{ ...mockRunProgress, raw: { current_ticket_id: 'TICKET-110' } }],
      chats: [mockChatSummary],
      artifacts: []
    });
    const { body } = render(TicketViews, {
      props: { state: 'ready', mode: 'list', list, selectedFilter: 'active' }
    });

    expect(body).toContain('Current ticket queue');
    expect(body).toContain('#110');
    expect(body).toContain('Implement typed UI API client and view models');
    expect(body).toContain('PMA chat');
    expect(body).toContain('running');
    expect(body).not.toContain('---');
  });

  it('renders ticket contract sections on detail pages', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        body: `## Goal
Users can browse tickets.

## Tasks
- Render list
- Render detail

## Acceptance criteria
- Contract preserved

## Tests
- Component tests
`
      },
      {
        tickets: [mockTicketSummary],
        runs: [mockRunProgress],
        chats: [mockChatSummary],
        artifacts: [mockArtifact]
      }
    );
    const { body } = render(TicketViews, {
      props: { state: 'ready', mode: 'detail', detail }
    });

    expect(body).toContain('Ticket contract');
    expect(body).toContain('Users can browse tickets.');
    expect(body).toContain('Render list');
    expect(body).toContain('Acceptance criteria');
    expect(body).toContain('Component tests');
  });

  it('renders execution timeline states and keeps debug secondary', () => {
    const detail = buildTicketDetailViewModel(
      mockTicketDetail,
      {
        tickets: [mockTicketSummary],
        runs: [{ ...mockRunProgress, status: 'failed', raw: { current_ticket_id: 'TICKET-110' } }],
        chats: [mockChatSummary],
        artifacts: [{ ...mockArtifact, kind: 'error', title: 'Build failed' }]
      }
    );
    const { body } = render(TicketViews, {
      props: { state: 'ready', mode: 'detail', detail }
    });

    expect(body).toContain('Execution timeline');
    expect(body).toContain('failed');
    expect(body).toContain('Retry run');
    expect(body).toContain('Raw logs/debug');
    expect(body).toContain('Surfaced artifacts');
  });
});
