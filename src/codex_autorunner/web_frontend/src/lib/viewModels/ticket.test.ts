import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketDetail, mockTicketSummary } from './mockData';
import {
  buildTicketDetailViewModel,
  buildTicketRepairChatCreatePayload,
  buildTicketRepairPrompt,
  buildTicketListViewModel,
  buildTicketUpdateContent,
  filterTicketRows,
  parseTicketContract,
  resolveTicketRouteId,
  ticketDetailFromSummary,
  ticketModelRawFromDetail
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

    expect(vm.defaultFilter).toBe('all');
    expect(vm.filters.map((f) => f.id)).toEqual(['all', 'open']);
    expect(filterTicketRows(vm.rows, 'all')).toHaveLength(2);
    expect(vm.title).toBe('Tickets');
    expect(vm.eyebrow).toBe('All-ticket projection');
    expect(vm.subtitle).toContain('Tickets without a registered owner');
    const activeRow = vm.rows.find((row) => row.id === mockTicketSummary.id);
    expect(activeRow).toMatchObject({
      numberLabel: '#110',
      title: mockTicketSummary.title,
      repoLabel: 'Worktree: worktree-1',
      currentRunState: 'running',
      chatHref: '/chats?chat=chat-1',
      modelLabel: 'configured model'
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

  it('does not expose stale pending stop-requested runs as queue actions', () => {
    const vm = buildTicketListViewModel(
      {
        tickets: [],
        runs: [
          {
            ...mockRunProgress,
            id: 'run-stale',
            status: 'waiting',
            raw: {
              id: 'run-stale',
              status: 'pending',
              stop_requested: true,
              repo_id: 'repo-1',
              action_policy: [
                {
                  action: 'stop',
                  enabled: true,
                  label: 'Stop',
                  method: 'POST',
                  route: '/api/flows/run-stale/stop',
                  surface_visibility: { queue: true }
                }
              ]
            }
          }
        ],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    expect(vm.queueRun).toBeNull();
    expect(vm.queueActions).toEqual([]);
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

  it('does not mark a fallback current ticket as working when the scoped flow is idle', () => {
    const vm = buildTicketListViewModel(
      {
        tickets: [
          {
            ...mockTicketSummary,
            id: 'TICKET-001',
            number: 1,
            title: 'First ticket',
            status: 'idle',
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            repoId: 'repo-1',
            worktreeId: null,
            raw: {}
          }
        ],
        runs: [],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    expect(vm.flowStatus).toMatchObject({
      status: 'idle',
      currentTicketId: 'TICKET-001'
    });
    expect(vm.rows[0].isCurrent).toBe(false);
  });

  it('keeps invalid tickets out of the failed filter while flagging attention', () => {
    const vm = buildTicketListViewModel({
      tickets: [
        {
          ...mockTicketSummary,
          id: 'TICKET-invalid',
          status: 'invalid',
          errors: ['frontmatter.agent is required']
        }
      ],
      runs: [],
      chats: [],
      artifacts: []
    });

    expect(vm.rows[0].needsAttention).toBe(true);
    expect(filterTicketRows(vm.rows, 'needs_attention')).toHaveLength(1);
    expect(filterTicketRows(vm.rows, 'failed')).toHaveLength(0);
    expect(filterTicketRows(vm.rows, 'open')).toHaveLength(1);
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
    expect(detail.linkedChatId).toBe('chat-1');
    expect(detail.actions.map((action) => action.label)).not.toContain('Open PMA chat');
    expect(detail.actions.map((action) => action.label)).toContain('Open chat');
    expect(detail.actions.map((action) => action.label)).toContain('Continue run');
    expect(detail.actions.find((action) => action.label === 'Raw logs/debug')?.secondary).toBe(true);
  });

  it('builds ticket detail transcript cards from linked chat timeline and live progress', () => {
    const detail = buildTicketDetailViewModel(
      mockTicketDetail,
      {
        tickets: [mockTicketSummary],
        runs: [mockRunProgress],
        chats: [mockChatSummary],
        artifacts: [],
        timeline: [
          {
            id: 'timeline-assistant-1',
            kind: 'assistant_message',
            orderKey: '00000001|message|timeline-assistant-1',
            timestamp: '2026-05-04T00:02:00Z',
            chatId: 'chat-1',
            turnId: 'turn-1',
            status: 'running',
            payload: { text: 'I am implementing the ticket now.' },
            raw: {}
          }
        ]
      }
    );

    expect(detail.chatTranscriptCards).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: 'message',
          message: expect.objectContaining({ text: 'I am implementing the ticket now.' })
        })
      ])
    );
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

    expect(detail.flowRunId).toBe('run-nested');
    expect(detail.runHref).toBe('/api/flows/run-nested/status');
    expect(detail.chatHref).toBe('/chats?chat=ticket%3ATICKET-110');
    expect(detail.actions.find((action) => action.label === 'Open chat')).toMatchObject({
      href: '/chats?chat=ticket%3ATICKET-110'
    });
    expect(detail.timeline.map((item) => item.id)).toContain('run-run-nested');
    expect(detail.timeline.find((item) => item.id === 'run-run-nested')?.href).toBe('/chats?chat=ticket%3ATICKET-110');
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

  it('reads stored model from ticket API frontmatter for the settings form', () => {
    const rawDetail = {
      ...mockTicketDetail,
      raw: {
        frontmatter: {
          ticket_id: 'TICKET-110',
          agent: 'codex',
          done: false,
          title: mockTicketDetail.title,
          model: 'gpt-5'
        }
      }
    };
    expect(ticketModelRawFromDetail(rawDetail)).toBe('gpt-5');
    const vm = buildTicketDetailViewModel(rawDetail, {
      tickets: [mockTicketSummary],
      runs: [],
      chats: [],
      artifacts: []
    });
    expect(vm.modelRaw).toBe('gpt-5');
    expect(vm.settingsSyncSignature).toContain('gpt-5');
  });

  it('changes settingsSyncSignature when persisted model/reasoning/agent fields change', () => {
    const a = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: {
          frontmatter: {
            ticket_id: 'TICKET-110',
            agent: 'codex',
            done: false,
            title: 't',
            model: 'gpt-5',
            reasoning: 'high'
          }
        }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );
    const b = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: {
          frontmatter: {
            ticket_id: 'TICKET-110',
            agent: 'opencode',
            done: false,
            title: 't',
            model: 'gpt-4o',
            reasoning: 'minimal'
          }
        },
        agentId: 'opencode'
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );
    expect(a.settingsSyncSignature).not.toBe(b.settingsSyncSignature);
  });

  it('writes model and reasoning into serialized ticket markdown on save', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: {
          frontmatter: {
            ticket_id: 'TICKET-110',
            agent: 'codex',
            done: false,
            title: 'Hello'
          }
        }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );
    const md = buildTicketUpdateContent(detail, {
      title: 'Hello',
      agent: 'codex',
      model: 'gpt-5-mini',
      reasoning: 'minimal',
      done: false,
      body: 'Body text'
    });
    expect(md).toContain('"gpt-5-mini"');
    expect(md).toContain('"minimal"');
    expect(md).toContain('Body text');
  });

  it('preserves manually edited frontmatter yaml while updating known settings', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        errors: ['frontmatter.done is required and must be a boolean.'],
        raw: {
          frontmatter: { title: 'Broken', agent: 'codex', done: 'nope', custom_flag: 'keep' },
          frontmatter_yaml: 'title: Broken\nagent: codex\ndone: nope\ncustom_flag: keep'
        }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );

    expect(detail.frontmatterEditableYaml).toContain('done: nope');
    const md = buildTicketUpdateContent(detail, {
      title: 'Fixed',
      agent: 'codex',
      model: '',
      reasoning: '',
      done: false,
      frontmatterYaml: 'title: Broken\nagent: codex\ndone: nope\ncustom_flag: keep',
      body: 'Body text'
    });

    expect(md).toContain('title: "Fixed"');
    expect(md).toContain('done: false');
    expect(md).toContain('custom_flag: keep');
    expect(md).toContain('Body text');
  });

  it('only upserts top-level frontmatter keys', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: {
          frontmatter: { title: 'Broken', agent: 'codex', done: false },
          frontmatter_yaml: 'title: Broken\nagent: codex\ndone: false\nnested:\n  title: keep nested title\n  done: keep nested done'
        }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );

    const md = buildTicketUpdateContent(detail, {
      title: 'Fixed',
      agent: 'opencode',
      model: '',
      reasoning: '',
      done: true,
      frontmatterYaml: detail.frontmatterEditableYaml,
      body: 'Body text'
    });

    expect(md).toContain('title: "Fixed"');
    expect(md).toContain('agent: "opencode"');
    expect(md).toContain('done: true');
    expect(md).toContain('  title: keep nested title');
    expect(md).toContain('  done: keep nested done');
  });

  it('builds PMA ticket repair chat payloads from ticket metadata', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        errors: ['frontmatter.done is required'],
        raw: {
          hub_root: '/hub',
          workspace_root: '/workspace/repo',
          repo_id: 'repo-1',
          frontmatter: { ticket_id: 'TICKET-110', agent: 'codex', done: false }
        }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );

    expect(buildTicketRepairChatCreatePayload(detail)).toEqual({
      agent: 'codex',
      name: 'Repair #110 frontmatter',
      scope_urn: 'worktree:repo-1/worktree-1'
    });
    expect(buildTicketRepairPrompt(detail)).toContain('Hub root: /hub');
    expect(buildTicketRepairPrompt(detail)).toContain('Workspace root: /workspace/repo');
    expect(buildTicketRepairPrompt(detail)).toContain('Ticket path: .codex-autorunner/tickets/TICKET-110.md');
    expect(buildTicketRepairPrompt(detail)).toContain('- frontmatter.done is required');
  });
});
