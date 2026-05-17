import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from './readModelStore';
import {
  loadScopedTicketDetailSession,
  loadScopedTicketListSession,
  renderScopedTicketCachedDetail,
  ticketFlowEventShouldReload
} from './scopedTicketSessions';

const now = '2026-05-17T12:00:00Z';

describe('scoped ticket sessions', () => {
  it('hydrates repo ticket list snapshots and action manifests', async () => {
    const store = new ReadModelEntityStore();
    const api = mockApi({
      repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot('repo-1'))),
      requestJson: vi.fn().mockResolvedValue(ok({ actions: [] }))
    });

    const result = await loadScopedTicketListSession(
      api,
      {
        kind: 'repo',
        resourceId: 'repo-1',
        apiBasePath: '/repos/repo-1/api/flows',
        displayLabel: 'repo'
      },
      { store }
    );

    expect(result.ok).toBe(true);
    expect(store.snapshot().ticketOrderByOwner['repo:repo-1']).toEqual(['t-1']);
    expect(store.snapshot().pmaRunOrderByOwner['repo:repo-1']).toEqual(['run-1']);
    expect(api.requestJson).toHaveBeenCalledWith(
      '/repos/repo-1/api/flows/ticket_flow/action-manifest?ui_kind=pma_web&resource_kind=repo&resource_id=repo-1'
    );
  });

  it('returns worktree legacy redirects with parent repo context', async () => {
    const store = new ReadModelEntityStore();
    const api = mockApi({
      worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot('wt-1', 'repo-1'))),
      requestJson: vi.fn().mockResolvedValue(ok({ actions: [] }))
    });

    const result = await loadScopedTicketListSession(
      api,
      {
        kind: 'worktree',
        resourceId: 'wt-1',
        apiBasePath: '/repos/wt-1/api/flows',
        displayLabel: 'worktree'
      },
      { currentPath: '/worktrees/wt-1/tickets', store }
    );

    expect(result.ok && result.parentRepoId).toBe('repo-1');
    expect(result.ok && result.redirectTo).toBe('/repos/repo-1/worktrees/wt-1/tickets');
    expect(store.snapshot().ticketOrderByOwner['worktree:wt-1']).toEqual(['t-1']);
  });

  it('renders cached detail from hydrated ticket summaries', () => {
    const store = new ReadModelEntityStore();
    store.replaceScopedTicketSummaries('repo:repo-1', [ticketSummary('t-1', 'Ticket One') as any]);

    const result = renderScopedTicketCachedDetail({ kind: 'repo', id: 'repo-1' }, 't-1', { store });

    expect(result?.detail.title).toBe('Ticket One');
    expect(result?.ownerKey).toBe('repo:repo-1');
  });

  it('normalizes ticket detail snapshots into detail view models', async () => {
    const store = new ReadModelEntityStore();
    const api = mockApi({
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot('t-1', 'repo-1')))
    });

    const result = await loadScopedTicketDetailSession(api, { kind: 'repo', id: 'repo-1' }, 't-1', { store });

    expect(result.ok && result.detail.title).toBe('Ticket One');
    expect(result.ok && result.currentRunId).toBe('run-1');
    expect(result.ok && result.dispatches).toEqual([{ message: 'queued' }]);
    expect(store.snapshot().ticketOrderByOwner['repo:repo-1']).toEqual(['t-1']);
  });

  it('centralizes terminal flow-event reload decisions', () => {
    expect(ticketFlowEventShouldReload({ status: 'completed' })).toBe(true);
    expect(ticketFlowEventShouldReload({ event_type: 'flow.terminal' })).toBe(true);
    expect(ticketFlowEventShouldReload({ status: 'running' })).toBe(false);
  });
});

function mockApi(overrides: Record<string, ReturnType<typeof vi.fn>> = {}) {
  return {
    requestJson: overrides.requestJson ?? vi.fn().mockResolvedValue(ok({})),
    readModels: {
      repoDetail: overrides.repoDetail ?? vi.fn().mockResolvedValue(ok(repoDetailSnapshot())),
      worktreeDetail: overrides.worktreeDetail ?? vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot())),
      ticketDetail: overrides.ticketDetail ?? vi.fn().mockResolvedValue(ok(ticketDetailSnapshot()))
    }
  } as any;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function repoDetailSnapshot(ownerId = 'repo-1'): RepoWorktreeDetailSnapshot {
  return detailSnapshot('repo', ownerId, null);
}

function worktreeDetailSnapshot(ownerId = 'wt-1', parentRepoId = 'repo-1'): RepoWorktreeDetailSnapshot {
  return detailSnapshot('worktree', ownerId, parentRepoId);
}

function detailSnapshot(
  ownerKind: 'repo' | 'worktree',
  ownerId: string,
  parentRepoId: string | null
): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, `${ownerKind}.detail`),
    ownerKind,
    ownerId,
    identity: {},
    parentLinks: parentRepoId ? { repo_id: parentRepoId } : {},
    topology: {},
    runtime: {},
    ticketQueue: [ticketSummary('t-1', 'Ticket One')],
    runQueue: [runSummary('run-1')],
    chatQueue: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: window(),
    runWindow: window(),
    chatWindow: window(),
    artifactWindow: window(),
    repair: repair(`/hub/read-models/${ownerKind}/${ownerId}`)
  };
}

function ticketDetailSnapshot(ticketId = 't-1', ownerId = 'repo-1'): TicketDetailSnapshot {
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
    repair: repair('/hub/read-models/tickets/t-1'),
    ticketDetail: { id: ticketId, title: 'Ticket One', status: 'running', run_id: 'run-1' },
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
