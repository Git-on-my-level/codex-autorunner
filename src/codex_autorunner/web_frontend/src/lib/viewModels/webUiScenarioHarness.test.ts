import { describe, expect, it } from 'vitest';
import {
  mapContextspaceDocument,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapTicketSummary,
  mapWorktreeSummary,
  type PmaChatSummary,
  type PmaRunProgress,
  type RepoSummary,
  type SurfaceArtifact,
  type TicketSummary,
  type WorktreeSummary
} from './domain';
import { buildPmaChatListEntries } from './pmaChat';
import {
  buildRepoWorktreeDetailViewModel,
  buildRepoWorktreeIndexViewModel,
  filterRepoWorktreeIndexRows
} from './repoWorktree';
import { buildSettingsViewModel } from './settings';
import { buildTicketDetailViewModel, buildTicketListViewModel, filterTicketRows } from './ticket';
import { buildContextspaceViewModel } from './contextspace';

type FixturePayload = {
  repos: Record<string, unknown>[];
  worktrees: Record<string, unknown>[];
  tickets: Record<string, unknown>[];
  runs: Record<string, unknown>[];
  chats: Record<string, unknown>[];
  contextspaceDocs: Record<string, unknown>[];
  artifacts: Record<string, unknown>[];
  settings: {
    session: Record<string, unknown>;
    agents: Record<string, unknown>[];
    modelCatalogs: Record<string, Record<string, unknown>[]>;
  };
};

function emptyHub(): FixturePayload {
  return {
    repos: [],
    worktrees: [],
    tickets: [],
    runs: [],
    chats: [],
    contextspaceDocs: [],
    artifacts: [],
    settings: {
      session: {
        model_overrides: {},
        effort_override: '',
        approval_policy: 'on-request',
        sandbox_mode: 'workspace-write'
      },
      agents: [{ id: 'codex', name: 'Codex', capabilities: ['chat', 'tickets'] }],
      modelCatalogs: { codex: [{ id: 'gpt-5.3-codex', label: 'GPT-5.3 Codex' }] }
    }
  };
}

function seededRepoWorktreeTicket(): FixturePayload {
  const payload = emptyHub();
  payload.repos = [
    {
      id: 'smoke-repo',
      name: 'smoke-repo',
      path: '/tmp/smoke-repo',
      status: 'idle',
      default_branch: 'main',
      worktree_count: 1,
      active_runs: 0,
      open_tickets: 1,
      last_activity_at: '2026-05-01T10:00:00Z',
      has_car_state: true
    }
  ];
  payload.worktrees = [
    {
      id: 'smoke-repo--review',
      repo_id: 'smoke-repo',
      name: 'smoke-repo--review',
      path: '/tmp/smoke-repo-review',
      branch: 'review',
      status: 'idle',
      active_runs: 0,
      open_tickets: 1,
      last_activity_at: '2026-05-01T10:05:00Z',
      has_car_state: true
    }
  ];
  payload.tickets = [ticketSummary()];
  payload.contextspaceDocs = [
    {
      id: 'active_context',
      name: 'active_context.md',
      kind: 'active_context',
      content: '# Durable shared context\n\nFixture context.',
      updated_at: '2026-05-01T10:02:00Z',
      is_pinned: true
    }
  ];
  return payload;
}

function chatListDetail(): FixturePayload {
  const payload = seededRepoWorktreeTicket();
  payload.chats = [
    {
      thread_target_id: 'chat-smoke-1',
      title: 'Smoke fixture chat',
      status: 'running',
      agent_id: 'codex',
      repo_id: 'smoke-repo',
      worktree_id: 'smoke-repo--review',
      current_ticket_id: 'TICKET-350-smoke-fixture',
      ticket_done: false,
      flow_type: 'ticket_flow',
      last_activity_at: '2026-05-01T10:10:00Z',
      progress_percent: 42
    },
    {
      thread_target_id: 'chat-unknown-status',
      title: 'Unknown status should normalize',
      status: 'mystery-new-state',
      agent_id: 'codex',
      repo_id: 'smoke-repo',
      last_activity_at: '2026-05-01T10:09:00Z'
    }
  ];
  return payload;
}

function largeListWindowing(): FixturePayload {
  const payload = seededRepoWorktreeTicket();
  payload.tickets = Array.from({ length: 120 }, (_, index) => {
    const number = 350 + index;
    return {
      ...ticketSummary(number, number % 5 === 0 ? 'done' : 'idle'),
      id: `TICKET-${number.toString().padStart(3, '0')}-smoke-fixture`,
      path: `.codex-autorunner/tickets/TICKET-${number.toString().padStart(3, '0')}-smoke-fixture.md`
    };
  });
  return payload;
}

function pmaRunning(): FixturePayload {
  const payload = chatListDetail();
  payload.runs = [runProgress('running', 'implementation', 0, 46)];
  return payload;
}

function pmaFinal(): FixturePayload {
  const payload = chatListDetail();
  payload.tickets = [ticketSummary(350, 'done', true)];
  payload.runs = [runProgress('done', 'final', 0, 100, true)];
  payload.artifacts = [
    {
      id: 'final-report',
      kind: 'final_report',
      title: 'Final report',
      summary: 'Scenario completed.',
      url: '/artifacts/final-report.md',
      created_at: '2026-05-01T10:30:00Z'
    }
  ];
  return payload;
}

function ticketSummary(number = 350, status = 'idle', done = false): Record<string, unknown> {
  return {
    id: `TICKET-${number.toString().padStart(3, '0')}-smoke-fixture`,
    number,
    title: 'TICKET-350-smoke-fixture',
    status,
    done,
    workspace_kind: 'worktree',
    workspace_id: 'smoke-repo--review',
    workspace_path: '/tmp/smoke-repo-review',
    repo_id: 'smoke-repo',
    worktree_id: 'smoke-repo--review',
    path: `.codex-autorunner/tickets/TICKET-${number.toString().padStart(3, '0')}-smoke-fixture.md`,
    ticket_path: `.codex-autorunner/tickets/TICKET-${number.toString().padStart(3, '0')}-smoke-fixture.md`,
    agent_id: 'codex',
    chat_key: 'chat-smoke-1',
    run_id: 'run-smoke-ticket-flow',
    updated_at: '2026-05-01T10:08:00Z',
    duration_seconds: 120,
    frontmatter: { title: 'TICKET-350-smoke-fixture', agent: 'codex', done },
    body: '## Goal\nExercise the Web Hub UI scenario harness.',
    errors: []
  };
}

function runProgress(status: string, phase: string, queueDepth: number, progressPercent: number, terminal = false): Record<string, unknown> {
  return {
    id: 'run-smoke-ticket-flow',
    chat_id: 'chat-smoke-1',
    status,
    work_status: status,
    terminal,
    stream_should_close: terminal,
    phase,
    queue_depth: queueDepth,
    progress_percent: progressPercent,
    started_at: '2026-05-01T10:00:00Z',
    last_event_id: 3,
    last_event_at: '2026-05-01T10:15:00Z',
    ticket_id: 'TICKET-350-smoke-fixture',
    resource_kind: 'worktree',
    resource_id: 'smoke-repo--review',
    repo_id: 'smoke-repo',
    worktree_id: 'smoke-repo--review'
  };
}

function mapped(payload: FixturePayload): {
  repos: RepoSummary[];
  worktrees: WorktreeSummary[];
  tickets: TicketSummary[];
  runs: PmaRunProgress[];
  chats: PmaChatSummary[];
  artifacts: SurfaceArtifact[];
} {
  return {
    repos: payload.repos.map(mapRepoSummary),
    worktrees: payload.worktrees.map(mapWorktreeSummary),
    tickets: payload.tickets.map(mapTicketSummary),
    runs: payload.runs.map(mapPmaRunProgress),
    chats: payload.chats.map(mapPmaChatSummary),
    artifacts: payload.artifacts.map(mapSurfaceArtifact)
  };
}

describe('fixture-backed Web UI scenario harness', () => {
  it('normalizes an empty hub into a bounded repo/worktree screen model', () => {
    const view = buildRepoWorktreeIndexViewModel(mapped(emptyHub()), 'all');

    expect(view.title).toBe('Repos');
    expect(view.rows).toEqual([]);
    expect(view.openTicketCount).toBe(0);
  });

  it('maps seeded repo and worktree route payloads into index and detail view models', () => {
    const source = mapped(seededRepoWorktreeTicket());
    const index = buildRepoWorktreeIndexViewModel(source, 'all');
    const detail = buildRepoWorktreeDetailViewModel(
      {
        ...source,
        contextspaceDocs: seededRepoWorktreeTicket().contextspaceDocs.map(mapContextspaceDocument)
      },
      'repo',
      'smoke-repo'
    );

    expect(index.rows.map((row) => row.id)).toContain('smoke-repo');
    expect(filterRepoWorktreeIndexRows(index.rows, 'smoke-repo', 'all').length).toBeGreaterThanOrEqual(1);
    expect(detail.title).toBe('smoke-repo');
    expect(detail.ticketIndexLabel).toBe('Repo tickets');
  });

  it('keeps large ticket queues bounded after contract mapping', () => {
    const source = mapped(largeListWindowing());
    const view = buildTicketListViewModel({ ...source, artifacts: [] });
    const openRows = filterTicketRows(view.rows, 'open');

    expect(view.rows).toHaveLength(120);
    expect(openRows.length).toBeLessThanOrEqual(96);
    expect(view.rows.slice(0, 50).map((row) => row.routeId)).toContain('TICKET-350-smoke-fixture');
  });

  it('normalizes unknown statuses and missing optional fields for chat lists', () => {
    const source = mapped(chatListDetail());
    const entries = buildPmaChatListEntries(source.chats);

    expect(source.chats.find((chat) => chat.id === 'chat-unknown-status')?.status).toBe('idle');
    expect(entries.length).toBeGreaterThan(0);
  });

  it('builds running worktree detail from PMA progress snapshots', () => {
    const source = mapped(pmaRunning());
    const detail = buildRepoWorktreeDetailViewModel(source, 'worktree', 'smoke-repo--review');

    expect(detail.title).toBe('smoke-repo--review');
    expect(detail.hasActiveRun).toBe(true);
    expect(detail.currentRuns[0]?.status).toBe('running');
  });

  it('keeps ticket detail repair and cursor-shaped snapshots idempotent', () => {
    const raw = pmaFinal();
    const source = mapped(raw);
    const detail = buildTicketDetailViewModel(mapTicketDetail(raw.tickets[0]), { ...source, timeline: [] });
    const cursorSnapshot = { next_cursor: null, count: source.tickets.length, route: '/hub/read-models/tickets/TICKET-350-smoke-fixture' };
    const repairSnapshot = { needs_repair: detail.needsRepair, errors: detail.errors };

    expect(detail.done).toBe(true);
    expect(JSON.parse(JSON.stringify(cursorSnapshot))).toEqual(cursorSnapshot);
    expect(JSON.parse(JSON.stringify(repairSnapshot))).toEqual(repairSnapshot);
  });

  it('maps contextspace and settings payloads without browser orchestration', () => {
    const raw = seededRepoWorktreeTicket();
    const source = mapped(raw);
    const contextspace = buildContextspaceViewModel(
      'smoke-repo--review',
      raw.contextspaceDocs.map(mapContextspaceDocument),
      source.repos,
      source.worktrees
    );
    const settings = buildSettingsViewModel(raw.settings);

    expect(contextspace.docs.map((doc) => doc.filename)).toContain('active_context.md');
    expect(settings.agents.map((agent) => agent.id)).toContain('codex');
    expect(settings.hub.map((item) => item.label)).toContain('Hub mode');
  });
});
