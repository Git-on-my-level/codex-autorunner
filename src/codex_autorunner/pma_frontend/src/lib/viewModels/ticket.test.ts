import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketDetail, mockTicketSummary } from './mockData';
import {
  buildTicketDetailViewModel,
  buildTicketListViewModel,
  filterTicketRows,
  parseTicketContract,
  resolveTicketRouteId,
  ticketDetailFromSummary
} from './ticket';

describe('ticket view models', () => {
  it('builds a needs-attention-first ticket list with run and chat context', () => {
    const vm = buildTicketListViewModel({
      tickets: [
        {
          ...mockTicketSummary,
          id: 'TICKET-111',
          number: 111,
          title: 'Done ticket',
          status: 'done',
          path: '.codex-autorunner/tickets/TICKET-111.md',
          chatKey: null
        },
        mockTicketSummary
      ],
      runs: [{ ...mockRunProgress, raw: { current_ticket: '.codex-autorunner/tickets/TICKET-110.md' } }],
      chats: [mockChatSummary],
      artifacts: []
    });

    expect(vm.defaultFilter).toBe('open');
    expect(vm.title).toBe('Tickets');
    expect(vm.eyebrow).toBe('All-ticket projection');
    expect(vm.subtitle).toContain('Tickets without a registered owner');
    const activeRow = vm.rows.find((row) => row.id === mockTicketSummary.id);
    expect(activeRow).toMatchObject({
      numberLabel: '#110',
      title: mockTicketSummary.title,
      repoLabel: 'Worktree: worktree-1',
      currentRunState: 'running',
      chatHref: '/chats?chat=chat-1'
    });
    expect(vm.workspaceFilters.map((filter) => filter.id)).toContain('worktree:worktree-1');
    expect(filterTicketRows(vm.rows, 'active')).toHaveLength(2);
    expect(filterTicketRows(vm.rows, 'active', 'worktree:worktree-1')).toHaveLength(2);
    expect(filterTicketRows(vm.rows, 'done_recent')).toHaveLength(1);
  });

  it('labels unscoped tickets honestly as current workspace fallback', () => {
    const vm = buildTicketListViewModel({
      tickets: [
        {
          ...mockTicketSummary,
          workspaceKind: 'unscoped',
          workspaceId: null,
          workspacePath: null,
          repoId: null,
          worktreeId: null,
          raw: {}
        }
      ],
      runs: [],
      chats: [],
      artifacts: []
    });

    expect(vm.rows[0]).toMatchObject({
      workspaceKind: 'unscoped',
      repoLabel: 'Needs owner repair',
      workspaceHref: null
    });
  });

  it('labels repo-scoped and worktree-scoped tickets from explicit resource ownership', () => {
    const vm = buildTicketListViewModel({
      tickets: [
        {
          ...mockTicketSummary,
          id: 'TICKET-201',
          title: 'Repo-owned QA',
          workspaceKind: 'unscoped',
          workspaceId: null,
          workspacePath: null,
          repoId: null,
          worktreeId: null,
          raw: { frontmatter: { resource_kind: 'repo', resource_id: 'repo-1' } }
        },
        {
          ...mockTicketSummary,
          id: 'TICKET-202',
          title: 'Worktree-owned QA',
          workspaceKind: 'unscoped',
          workspaceId: null,
          workspacePath: null,
          repoId: null,
          worktreeId: null,
          raw: { resource_kind: 'worktree', resource_id: 'worktree-1' }
        }
      ],
      runs: [],
      chats: [],
      artifacts: []
    });

    expect(vm.rows.map((row) => row.repoLabel)).toEqual(['Repo: repo-1', 'Worktree: worktree-1']);
    expect(vm.rows.map((row) => row.workspaceHref)).toEqual(['/repos/repo-1', '/worktrees/worktree-1']);
    expect(vm.workspaceFilters.map((filter) => filter.id)).toEqual([
      'all',
      'repo:repo-1',
      'worktree:worktree-1'
    ]);
  });

  it('keeps scoped ticket queues in ticket order while exposing the owner run', () => {
    const vm = buildTicketListViewModel(
      {
        tickets: [
          {
            ...mockTicketSummary,
            id: 'TICKET-002',
            number: 2,
            title: 'Waiting follow-up',
            status: 'waiting',
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            raw: {}
          },
          {
            ...mockTicketSummary,
            id: 'TICKET-001',
            number: 1,
            title: 'First ticket',
            status: 'idle',
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            raw: {}
          }
        ],
        runs: [{ ...mockRunProgress, id: 'run-repo-1', raw: { resource_kind: 'repo', resource_id: 'repo-1' } }],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    expect(vm.rows.map((row) => row.numberLabel)).toEqual(['#1', '#2']);
    expect(vm.queueRun).toMatchObject({ id: 'run-repo-1', status: 'running' });
  });

  it('parses ticket contract sections from markdown', () => {
    const sections = parseTicketContract(`Intro note

## Tasks
- Build list
- Build detail

## Acceptance criteria
- Contract rendered
`);

    expect(sections.map((section) => section.title)).toEqual(['Tasks', 'Acceptance criteria', 'Notes']);
    expect(sections[0].items).toEqual(['Build list', 'Build detail']);
  });

  it('builds ticket detail with contract, timeline, artifacts, and contextual actions', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        body: `## Goal
Users can inspect tickets.

## Tasks
- Render contract

## Tests
- Component coverage
`
      },
      {
        tickets: [mockTicketSummary],
        runs: [{ ...mockRunProgress, status: 'waiting', raw: { current_ticket_id: 'TICKET-110' } }],
        chats: [mockChatSummary],
        artifacts: [mockArtifact]
      },
      new Date('2026-05-04T00:03:00Z')
    );

    expect(detail.goal).toContain('Users can inspect tickets');
    expect(detail.repoLabel).toBe('Worktree: worktree-1');
    expect(detail.workspaceHref).toBe('/repos/repo-1/worktrees/worktree-1');
    expect(detail.timeline.map((item) => item.title)).toContain('waiting');
    expect(detail.artifacts[0]).toMatchObject({ kind: 'preview_url' });
    expect(detail.actions.map((action) => action.label)).toContain('Open PMA chat');
    expect(detail.actions.map((action) => action.label)).toContain('Continue run');
    expect(detail.actions.find((action) => action.label === 'Raw logs/debug')?.secondary).toBe(true);
  });

  it('matches ticket runs from nested ticket engine state', () => {
    const detail = buildTicketDetailViewModel(
      mockTicketDetail,
      {
        tickets: [mockTicketSummary],
        runs: [
          {
            ...mockRunProgress,
            id: 'run-nested',
            chatId: null,
            raw: { state: { ticket_engine: { current_ticket_id: mockTicketSummary.id } } }
          }
        ],
        chats: [],
        artifacts: []
      },
      new Date('2026-01-01T00:00:00Z')
    );

    expect(detail.runHref).toBe('/api/flows/run-nested/status');
    expect(detail.timeline.map((item) => item.id)).toContain('run-run-nested');
  });

  it('resolves ticket detail route ids emitted by the ticket list', () => {
    const nonIndexed = {
      ...mockTicketSummary,
      id: 'tkt_non_indexed',
      number: null,
      path: '.codex-autorunner/tickets/manual-ticket.md',
      raw: { body: 'Manual ticket body' }
    };
    const tickets = [mockTicketSummary, nonIndexed];

    expect(resolveTicketRouteId(tickets, '110')?.id).toBe(mockTicketSummary.id);
    expect(resolveTicketRouteId(tickets, 'tkt_non_indexed')?.id).toBe('tkt_non_indexed');
    expect(resolveTicketRouteId(tickets, encodeURIComponent('.codex-autorunner/tickets/manual-ticket.md'))?.id).toBe('tkt_non_indexed');
    expect(ticketDetailFromSummary(nonIndexed)).toMatchObject({
      id: 'tkt_non_indexed',
      number: null,
      body: 'Manual ticket body'
    });
  });
});
