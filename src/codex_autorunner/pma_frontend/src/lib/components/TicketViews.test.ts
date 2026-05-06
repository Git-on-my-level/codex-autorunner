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

    expect(body).toContain('All tickets');
    expect(body).toContain('All workspaces');
    expect(body).toContain('Worktree worktree-1');
    expect(body).toContain('Worktree: worktree-1');
    expect(body).toContain('#110');
    expect(body).toContain('Implement typed UI API client and view models');
    expect(body).toContain('PMA chat');
    expect(body).toContain('running');
    expect(body).not.toContain('title:');
  });

  it('renders sparse ticket-list filter empty states', () => {
    const list = buildTicketListViewModel({
      tickets: [{ ...mockTicketSummary, status: 'done' }],
      runs: [],
      chats: [],
      artifacts: []
    });
    const { body } = render(TicketViews, {
      props: { state: 'ready', mode: 'list', list, selectedFilter: 'active' }
    });

    expect(body).toContain('No tickets in this view');
    expect(body).toContain('Switch filters or ask PMA to create the next scoped ticket for the current CAR work.');
    expect(body).toContain('Tickets without a registered owner are flagged for ownership repair');
  });

  it('renders scoped queue status, create, reorder, and row affordances', () => {
    const list = buildTicketListViewModel(
      {
        tickets: [
          {
            ...mockTicketSummary,
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            repoId: 'repo-1',
            worktreeId: null,
            raw: { body: 'Implement the current ticket body preview.' }
          },
          {
            ...mockTicketSummary,
            id: 'TICKET-111',
            number: 111,
            title: 'Follow-up polish',
            status: 'waiting',
            path: '.codex-autorunner/tickets/TICKET-111.md',
            ticketPath: '.codex-autorunner/tickets/TICKET-111.md',
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            repoId: 'repo-1',
            worktreeId: null,
            raw: {}
          }
        ],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket: '.codex-autorunner/tickets/TICKET-110.md', turn_count: 4 } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: null }],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    const { body } = render(TicketViews, {
      props: {
        state: 'ready',
        mode: 'list',
        list,
        selectedFilter: 'open',
        onCreateTicket: async () => true,
        onReorderTicket: async () => true
      }
    });

    expect(body).toContain('Ticket flow controls');
    expect(body).toContain('Done/total');
    expect(body).toContain('4');
    expect(body).toContain('Create ticket');
    expect(body).toContain('working-badge');
    expect(body).toContain('Implement the current ticket body preview.');
    expect(body).toContain('+80 -5 4 files');
    expect(body).toContain('2m 0s');
    expect(body).toContain('Move #110 down');
    expect(body).not.toContain('All workspaces');
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

    expect(body).toContain('Ticket settings');
    expect(body).toContain('Agent');
    expect(body).toContain('Worktree: worktree-1');
    expect(body).toContain('href="/worktrees/worktree-1"');
    expect(body).toContain('Back to worktree tickets');
    expect(body).toContain('href="/worktrees/worktree-1/tickets"');
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

  it('renders sparse ticket-detail side panels without raw debug as primary content', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        chatKey: null,
        runId: null,
        body: '## Goal\nKeep sparse tickets readable.',
        artifacts: []
      },
      {
        tickets: [],
        runs: [],
        chats: [],
        artifacts: []
      }
    );
    const { body } = render(TicketViews, {
      props: { state: 'ready', mode: 'detail', detail }
    });

    expect(body).toContain('No artifacts surfaced');
    expect(body).toContain('Screenshots, previews, files, and test summaries will appear after PMA work produces them.');
    expect(body).toContain('No linked PMA chat');
    expect(body).toContain('A ticket-linked conversation appears here after PMA starts discussing this ticket.');
    expect(body).not.toContain('Raw logs/debug');
  });
});
