import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketDetail, mockTicketSummary } from './mockData';
import {
  buildTicketDetailViewModel,
  buildTicketListViewModel,
  filterTicketRows,
  parseTicketContract
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

    expect(vm.defaultFilter).toBe('needs_attention');
    expect(vm.rows[0]).toMatchObject({
      numberLabel: '#110',
      title: mockTicketSummary.title,
      currentRunState: 'running',
      chatHref: '/pma?chat=chat-1'
    });
    expect(filterTicketRows(vm.rows, 'active')).toHaveLength(1);
    expect(filterTicketRows(vm.rows, 'done_recent')).toHaveLength(1);
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
});
