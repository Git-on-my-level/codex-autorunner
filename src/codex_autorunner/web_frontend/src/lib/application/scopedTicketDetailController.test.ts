import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiResult, JsonRecord } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import {
  createScopedTicketDetailController,
  type ScopedTicketDetailApi,
  type ScopedTicketDetailRoute
} from './scopedTicketDetailController';

const now = '2026-05-18T12:00:00Z';

describe('scoped ticket detail controller', () => {
  it('loads repo detail, opens a repo-scoped flow stream, and reloads after terminal events', async () => {
    const harness = createHarness({ route: repoRoute('repo-1', '1') });

    await harness.controller.loadTicketDetail();

    expect(harness.api.readModels.ticketDetail).toHaveBeenCalledWith('1', { kind: 'repo', id: 'repo-1' });
    expect(harness.openFlowRunEventSource).toHaveBeenCalledWith(
      'run-1',
      { repo: 'repo-1' },
      expect.objectContaining({ onEvent: expect.any(Function), onError: expect.any(Function) })
    );

    harness.emitFlowEvent({ id: 'evt-1', payload: { status: 'completed' } });

    expect(harness.api.readModels.ticketDetail).toHaveBeenCalledTimes(2);
    expect(harness.closedStreams).toBe(1);
  });

  it('uses worktree route APIs and invalidates worktree tags when saving', async () => {
    const harness = createHarness({ route: worktreeRoute('wt-1', '1') });

    await harness.controller.loadTicketDetail();
    await harness.controller.saveTicket({
      title: 'Updated',
      agent: 'codex',
      model: '',
      reasoning: '',
      done: true,
      body: '## Goal\n\n- Updated'
    });

    expect(harness.api.ticketFlow.updateTicket).toHaveBeenCalledWith(
      1,
      expect.stringContaining('title: "Updated"'),
      { worktree: 'wt-1' }
    );
    expect(harness.invalidateTags).toHaveBeenCalledWith([
      'entity:ticket:1',
      'entity:ticket:index',
      'entity:worktree:wt-1'
    ]);
  });

  it('preserves worktree legacy redirects from the scoped session loader', async () => {
    const harness = createHarness({
      route: worktreeRoute('wt-1', '1', '/worktrees/wt-1/tickets/1'),
      ticketSnapshot: ticketDetailSnapshot('1', 'wt-1', { base_repo_id: 'repo-1' })
    });

    await harness.controller.loadTicketDetail();

    expect(harness.navigate).toHaveBeenCalledWith('/repos/repo-1/worktrees/wt-1/tickets/1', { replaceState: true });
  });

  it('ignores stale detail responses after the route changes', async () => {
    const first = deferred<ApiResult<TicketDetailSnapshot>>();
    const harness = createHarness({ route: repoRoute('repo-1', '1') });
    harness.api.readModels.ticketDetail.mockImplementation((ticketId: string, owner: { id: string }) => {
      if (owner.id === 'repo-1') return first.promise;
      return Promise.resolve(ok(ticketDetailSnapshot(ticketId, owner.id, { title: 'Second Ticket' })));
    });

    const staleLoad = harness.controller.loadTicketDetail();
    harness.controller.setRoute(repoRoute('repo-2', '2'));
    await tick();
    first.resolve(ok(ticketDetailSnapshot('1', 'repo-1', { title: 'Stale Ticket' })));
    await staleLoad;

    expect(harness.controller.state.ownerScope.id).toBe('repo-2');
    expect(harness.controller.state.detail?.id).toBe('2');
  });

  it('sends repo flow commands through the owner runtime path', async () => {
    const harness = createHarness({ route: repoRoute('repo-1', '1') });
    await harness.controller.loadTicketDetail();

    await harness.controller.runCommand('resume');
    await harness.controller.runCommand('bootstrap');

    expect(harness.api.requestJson).toHaveBeenNthCalledWith(1, '/repos/repo-1/api/flows/run-1/resume', {
      method: 'POST',
      body: undefined
    });
    expect(harness.api.requestJson).toHaveBeenNthCalledWith(2, '/repos/repo-1/api/flows/ticket_flow/bootstrap', {
      method: 'POST',
      body: { once: false }
    });
  });

  it('loads agent model picker support once mounted', async () => {
    const harness = createHarness({
      agents: [
        { id: 'codex', capability_projection: { actions: { list_models: { allowed: true } } } },
        { id: 'pma', capability_projection: { actions: { list_models: { allowed: false } } } }
      ]
    });
    const states: JsonRecord[] = [];
    harness.controller.subscribe((state) => states.push(state as unknown as JsonRecord));

    harness.controller.mount();
    await tick();
    await tick();

    expect(harness.api.pma.listAgents).toHaveBeenCalled();
    expect(harness.api.pma.listAgentModels).toHaveBeenCalledWith('codex');
    expect(harness.controller.state.agents).toHaveLength(2);
    expect(harness.controller.state.modelCatalogs.codex).toEqual([{ id: 'gpt-5.5' }]);
    expect(states.length).toBeGreaterThan(1);
  });

  it('creates a PMA repair chat and navigates to it with owner invalidation tags', async () => {
    const harness = createHarness({ route: repoRoute('repo-1', '1') });
    await harness.controller.loadTicketDetail();

    await harness.controller.repairWithPma(harness.controller.state.detail!);

    expect(harness.api.pma.createChat).toHaveBeenCalledWith(expect.objectContaining({ name: expect.stringContaining('Repair') }));
    expect(harness.api.pma.sendMessage).toHaveBeenCalledWith('chat-1', expect.objectContaining({ message: expect.stringContaining('Ticket path') }));
    expect(harness.invalidateTags).toHaveBeenCalledWith([
      'entity:chat:index',
      'entity:chat:chat-1',
      'entity:ticket:1',
      'entity:repo:repo-1'
    ]);
    expect(harness.navigate).toHaveBeenCalledWith('/chats?chat=chat-1');
  });
});

function createHarness(options: {
  route?: ScopedTicketDetailRoute;
  ticketSnapshot?: TicketDetailSnapshot;
  agents?: JsonRecord[];
} = {}) {
  const store = new ReadModelEntityStore();
  const api = {
    requestJson: vi.fn().mockResolvedValue(ok({})),
    readModels: {
      repoDetail: vi.fn(),
      worktreeDetail: vi.fn(),
      ticketDetail: vi.fn().mockResolvedValue(ok(options.ticketSnapshot ?? ticketDetailSnapshot()))
    },
    ticketFlow: {
      updateTicket: vi.fn().mockResolvedValue(ok({}))
    },
    pma: {
      listAgents: vi.fn().mockResolvedValue(ok({ agents: options.agents ?? [], default: 'codex', defaults: {} })),
      listAgentModels: vi.fn().mockResolvedValue(ok([{ id: 'gpt-5.5' }])),
      createChat: vi.fn().mockResolvedValue(ok({ id: 'chat-1' })),
      sendMessage: vi.fn().mockResolvedValue(ok({ id: 'turn-1' }))
    }
  } as unknown as MockApi;
  let latestOptions: { onEvent: (event: { id: string | null; payload: JsonRecord }) => void } | null = null;
  let closedStreams = 0;
  const openFlowRunEventSource = vi.fn((_runId, _owner, options) => {
    latestOptions = options;
    return {
      close: () => {
        closedStreams += 1;
      }
    };
  });
  const invalidateTags = vi.fn(async () => undefined);
  const navigate = vi.fn(async () => undefined);
  const controller = createScopedTicketDetailController({
    api,
    route: options.route ?? repoRoute('repo-1', '1'),
    store,
    openFlowRunEventSource,
    invalidateTags,
    navigate
  });
  return {
    api,
    controller,
    openFlowRunEventSource,
    invalidateTags,
    navigate,
    emitFlowEvent: (event: { id: string | null; payload: JsonRecord }) => latestOptions?.onEvent(event),
    get closedStreams() {
      return closedStreams;
    }
  };
}

function repoRoute(repoId: string, ticketId: string): ScopedTicketDetailRoute {
  return { ownerScope: { kind: 'repo', id: repoId }, ticketId };
}

function worktreeRoute(worktreeId: string, ticketId: string, currentPath?: string): ScopedTicketDetailRoute {
  return { ownerScope: { kind: 'worktree', id: worktreeId, parentRepoId: null }, ticketId, currentPath };
}

function ticketDetailSnapshot(ticketId = '1', ownerId = 'repo-1', raw: JsonRecord = {}): TicketDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'ticket.detail.snapshot',
    cursor: cursor(1, 'ticket.detail'),
    ticket: {
      ticketId,
      routeId: '1',
      title: 'Ticket One',
      status: 'running',
      ownerKind: 'repo',
      ownerId,
      agent: 'codex',
      model: 'gpt-5.5',
      done: false,
      updatedAt: now
    },
    siblings: [],
    linkedRun: null,
    linkedChats: [],
    artifacts: [],
    dispatchWindow: window(),
    dispatches: [{ message: 'queued' }],
    repair: repair('/hub/read-models/tickets/1'),
    ticketDetail: {
      id: ticketId,
      route_id: '1',
      title: 'Ticket One',
      status: 'running',
      run_id: 'run-1',
      path: '.codex-autorunner/tickets/TICKET-001.md',
      body: '## Goal\n\n- Existing',
      frontmatter: { title: 'Ticket One', agent: 'codex', done: false },
      workspace_path: '/workspace/repo',
      hub_root: '/hub',
      ...raw
    },
    ticketQueue: [ticketSummary(ticketId, 'Ticket One')],
    runQueue: [runSummary('run-1')],
    chatQueue: []
  };
}

function ticketSummary(id: string, title: string) {
  return {
    id,
    route_id: id,
    number: 1,
    title,
    status: 'running',
    done: false,
    raw: { id, title }
  };
}

function runSummary(id: string) {
  return {
    id,
    run_id: id,
    flow_type: 'ticket_flow',
    status: 'running',
    started_at: now,
    raw: { id, run_id: id, status: 'running' }
  };
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function cursor(sequence: number, source = 'test'): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt: now };
}

function window() {
  return { limit: 20, totalEstimate: 1, totalIsExact: true };
}

function repair(snapshotRoute: string) {
  return {
    snapshotRoute,
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type MockApi = ScopedTicketDetailApi & {
  requestJson: ReturnType<typeof vi.fn>;
  readModels: {
    repoDetail: ReturnType<typeof vi.fn>;
    worktreeDetail: ReturnType<typeof vi.fn>;
    ticketDetail: ReturnType<typeof vi.fn>;
  };
  ticketFlow: {
    updateTicket: ReturnType<typeof vi.fn>;
  };
  pma: {
    listAgents: ReturnType<typeof vi.fn>;
    listAgentModels: ReturnType<typeof vi.fn>;
    createChat: ReturnType<typeof vi.fn>;
    sendMessage: ReturnType<typeof vi.fn>;
  };
};
