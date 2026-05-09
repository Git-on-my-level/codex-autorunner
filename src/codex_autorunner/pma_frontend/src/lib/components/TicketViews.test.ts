import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import TicketViews from './TicketViews.svelte';
import { mockArtifact, mockChatSummary, mockRunProgress, mockTicketDetail, mockTicketSummary } from '$lib/viewModels/mockData';
import { buildTicketDetailViewModel, buildTicketListViewModel } from '$lib/viewModels/ticket';

const codexAgent = {
  id: 'codex',
  name: 'Codex',
  capability_projection: { actions: { list_models: { allowed: true, missing_capabilities: [] } } }
};

const hermesAgent = {
  id: 'hermes',
  name: 'Hermes',
  capability_projection: { actions: { list_models: { allowed: false, missing_capabilities: ['model_listing'] } } }
};

const codexModels = [
  { id: 'gpt-5', label: 'GPT-5', reasoning_options: ['medium', 'high'] },
  { id: 'gpt-5-mini', label: 'GPT-5 Mini', reasoning_options: [] }
];

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

    expect(body).toContain('Tickets');
    expect(body).toContain('#110');
    expect(body).toContain('Implement typed UI API client and view models');
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
        onReorderTicket: async () => true
      }
    });

    expect(body).toContain('href="/repos/repo-1/tickets/new"');

    expect(body).toContain('Ticket flow controls');
    expect(body).toContain('+ New ticket');
    expect(body).toContain('working-badge');
    expect(body).toContain('Implement the current ticket body preview.');
    expect(body).toContain('+80 -5 4 files');
    expect(body).toContain('2m 0s');
    expect(body).toContain('Drag #110 to reorder');
    expect(body).not.toContain('Move #110 down');
    expect(body).not.toContain('Move</span>');
    expect(body).not.toContain('All workspaces');
  });

  it('builds queue controls from backend action policy data', () => {
    const list = buildTicketListViewModel(
      {
        tickets: [{ ...mockTicketSummary, workspaceKind: 'repo', workspaceId: 'repo-1', repoId: 'repo-1', worktreeId: null }],
        runs: [
          {
            ...mockRunProgress,
            id: 'run-policy',
            raw: {
              repo_id: 'repo-1',
              action_policy: [
                { action: 'start', enabled: false, label: 'Start queue', disabled_reason: 'Ticket flow is already active', surface_visibility: { queue: true } },
                { action: 'stop', enabled: true, label: 'Stop', route: '/api/flows/run-policy/stop', surface_visibility: { queue: true } },
                { action: 'restart', enabled: false, label: 'Restart', route: '/api/flows/run-policy/restart', requires_confirmation: true, disabled_reason: 'No restartable flow run', surface_visibility: { queue: true } }
              ]
            }
          }
        ],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    expect(list.queueActions).toEqual([
      { action: 'start', enabled: false, label: 'Start queue', requiresConfirmation: false, disabledReason: 'Ticket flow is already active', method: 'POST', route: null },
      { action: 'stop', enabled: true, label: 'Stop', requiresConfirmation: false, disabledReason: null, method: 'POST', route: '/api/flows/run-policy/stop' },
      { action: 'restart', enabled: false, label: 'Restart', requiresConfirmation: true, disabledReason: 'No restartable flow run', method: 'POST', route: '/api/flows/run-policy/restart' }
    ]);
  });

  it('does not invent queue controls when backend action state is unavailable', () => {
    const list = buildTicketListViewModel(
      {
        tickets: [{ ...mockTicketSummary, workspaceKind: 'repo', workspaceId: 'repo-1', repoId: 'repo-1', worktreeId: null }],
        runs: [{ ...mockRunProgress, id: 'run-without-policy', raw: { repo_id: 'repo-1' } }],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' }
    );

    expect(list.queueActions).toEqual([]);
  });

  it('builds queue controls from backend action manifest data', () => {
    const list = buildTicketListViewModel(
      {
        tickets: [{ ...mockTicketSummary, workspaceKind: 'repo', workspaceId: 'repo-1', repoId: 'repo-1', worktreeId: null }],
        runs: [{ ...mockRunProgress, id: 'run-policy', raw: { repo_id: 'repo-1' } }],
        chats: [],
        artifacts: []
      },
      { kind: 'repo', id: 'repo-1' },
      {
        actions: [
          { action_id: 'ticket_flow.start', enabled: false, label: 'Start queue', disabled_reason: 'Ticket flow is already active', method: 'POST', route: '/api/flows/ticket_flow/bootstrap' },
          { action_id: 'ticket_flow.stop', enabled: true, label: 'Stop', disabled_reason: null, method: 'POST', route: '/api/flows/run-policy/stop' },
          { action_id: 'ticket_flow.restart', enabled: false, label: 'Restart', requires_confirmation: true, disabled_reason: 'No restartable flow run', method: 'POST', route: '/api/flows/run-policy/restart' }
        ]
      }
    );

    expect(list.queueActions).toEqual([
      { action: 'start', enabled: false, label: 'Start queue', requiresConfirmation: false, disabledReason: 'Ticket flow is already active', method: 'POST', route: '/api/flows/ticket_flow/bootstrap' },
      { action: 'stop', enabled: true, label: 'Stop', requiresConfirmation: false, disabledReason: null, method: 'POST', route: '/api/flows/run-policy/stop' },
      { action: 'restart', enabled: false, label: 'Restart', requiresConfirmation: true, disabledReason: 'No restartable flow run', method: 'POST', route: '/api/flows/run-policy/restart' }
    ]);
  });

  it('renders ticket contract sections on detail pages', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: { frontmatter: { agent: 'codex', model: 'gpt-5', reasoning: 'high' } },
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
        artifacts: [mockArtifact],
        timeline: [
          {
            id: 'assistant-1',
            kind: 'assistant_message',
            orderKey: '00000001|message|assistant-1',
            timestamp: '2026-05-04T00:02:00Z',
            chatId: 'chat-1',
            turnId: 'turn-1',
            status: 'done',
            payload: { text: 'Ticket implementation is in progress.' },
            raw: {}
          }
        ]
      }
    );
    const { body } = render(TicketViews, {
      props: {
        state: 'ready',
        mode: 'detail',
        detail,
        agents: [codexAgent],
        modelCatalogs: { codex: codexModels }
      }
    });

    expect(body).toContain('Agent');
    expect(body).toContain('Model');
    expect(body).toContain('Reasoning');
    expect(body).toContain('GPT-5');
    expect(body).toContain('high');
    expect(body).toContain('Back to worktree tickets');
    expect(body).toContain('href="/repos/repo-1/worktrees/worktree-1/tickets"');
    expect(body).toContain('Users can browse tickets.');
    expect(body).toContain('Render list');
    expect(body).toContain('Acceptance criteria');
    expect(body).toContain('Component tests');
    expect(body).toContain('running');
    expect(body).not.toContain('Ticket contract loaded');
    expect(body).not.toContain('Preview ready');
    expect(body).not.toContain('Linked PMA chat history');
    expect(body).not.toContain('Ticket implementation is in progress.');
  });

  it('hides reasoning picker when the catalog model disables reasoning support', () => {
    const glmModels = [
      {
        id: 'zai-coding-plan/glm-4.7',
        label: 'GLM 4.7',
        supports_reasoning: false,
        reasoning_options: ['minimal', 'high']
      }
    ];
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: { frontmatter: { agent: 'codex', model: 'zai-coding-plan/glm-4.7' } }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );
    const { body } = render(TicketViews, {
      props: {
        state: 'ready',
        mode: 'detail',
        detail,
        agents: [codexAgent],
        modelCatalogs: { codex: glmModels }
      }
    });
    expect(body).toContain('Model');
    expect(body).not.toContain('<span>Reasoning</span>');
  });

  it('hides ticket model and reasoning pickers when the selected agent lacks model-listing support', () => {
    const detail = buildTicketDetailViewModel(
      {
        ...mockTicketDetail,
        raw: { frontmatter: { agent: 'hermes', model: 'manual-model', reasoning: 'high' } }
      },
      { tickets: [mockTicketSummary], runs: [], chats: [], artifacts: [] }
    );

    const { body } = render(TicketViews, {
      props: {
        state: 'ready',
        mode: 'detail',
        detail,
        agents: [hermesAgent],
        modelCatalogs: {}
      }
    });

    expect(body).toContain('Agent');
    expect(body).toContain('hermes');
    expect(body).not.toContain('aria-label="Model"');
    expect(body).not.toContain('aria-label="Reasoning"');
    expect(body).not.toContain('manual-model');
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

    expect(body).toContain('failed');
    expect(body).toContain('Retry run');
    expect(body).toContain('Raw logs/debug');
    expect(body).not.toContain('Execution timeline');
    expect(body).not.toContain('Surfaced artifacts');
  });

  it('renders ticket-flow worker activity separately from linked PMA chat history', () => {
    const detail = buildTicketDetailViewModel(mockTicketDetail, {
      tickets: [mockTicketSummary],
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      artifacts: []
    });
    const { body } = render(TicketViews, {
      props: {
        state: 'ready',
        mode: 'detail',
        detail,
        workerActivity: {
          items: [
            {
              id: 'live-worker-output',
              title: 'Live worker output',
              summary: null,
              detail: 'Running pytest...',
              status: 'running',
              timestamp: '2026-05-04T00:02:00Z'
            }
          ]
        }
      }
    });

    expect(body).toContain('Worker output');
    expect(body).toContain('Running pytest...');
    expect(body).not.toContain('Streaming output and ticket-linked chat history appear here once PMA starts working this ticket.');
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

    expect(body).not.toContain('No artifacts surfaced');
    expect(body).not.toContain('Screenshots, previews, files, and test summaries will appear after PMA work produces them.');
    expect(body).not.toContain('No linked PMA chat');
    expect(body).not.toContain('A ticket-linked conversation appears here after PMA starts discussing this ticket.');
    expect(body).not.toContain('Raw logs/debug');
  });
});
