import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import type { TicketSummary } from '$lib/viewModels/domain';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';
import { importRouteLoader } from '$lib/test/importRouteLoader';

const now = '2026-05-11T12:00:00Z';

describe('/tickets/[ticketId] route load', () => {
  it('loads ticket index and returns result', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const summaries = [ticketSummary('t-1', 'Ticket A', 'repo', 'repo-1')];
    const client = mockClient({
      ticketIndex: vi.fn().mockResolvedValue(ok(summaries)),
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot('t-1')))
    });
    const { loadTicketDetailRoute } = await importPageLoad(true);

    const result = await loadTicketDetailRoute({ ticketId: 't-1', depends, loaderOptions: { store, client, blocking: true } });

    expect(result.ticketId).toBe('t-1');
    expect(result.indexResult.status).toBe('fetched');
    expect(result.detailResult?.status).toBe('fetched');
    expect(depends).toHaveBeenCalledWith('entity:ticket:index');
    expect(client.ticketDetail).toHaveBeenCalledWith('t-1', { kind: 'repo', id: 'repo-1' });
  });

  it('resolves ticket detail owner from the matching ticket summary entry', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const summaries = [
      ticketSummary('t-other', 'Other', 'repo', 'repo-wrong'),
      ticketSummary('t-1', 'Mine', 'repo', 'repo-right')
    ];
    const client = mockClient({
      ticketIndex: vi.fn().mockResolvedValue(ok(summaries)),
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot('t-1')))
    });
    const { loadTicketDetailRoute } = await importPageLoad(true);

    await loadTicketDetailRoute({ ticketId: 't-1', depends, loaderOptions: { store, client, blocking: true } });

    expect(client.ticketDetail).toHaveBeenCalledWith('t-1', { kind: 'repo', id: 'repo-right' });
  });

  it('returns cached ticket index immediately and refreshes in the background', async () => {
    const store = new ReadModelEntityStore();
    store.replaceScopedTicketSummaries('all', [ticketSummary('t-1', 'Cached')]);
    const client = mockClient();
    const { loadTicketDetailRoute } = await importPageLoad(true);

    const result = await loadTicketDetailRoute({ ticketId: 't-1', loaderOptions: { store, client } });

    expect(result.indexResult).toEqual({ status: 'cache-hit', tags: ['entity:ticket:index'] });
    expect(client.ticketIndex).toHaveBeenCalled();
  });

  it('returns cold and defers detail lookup when the ticket index is missing', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const { loadTicketDetailRoute } = await importPageLoad(true);

    const result = await loadTicketDetailRoute({ ticketId: 't-1', loaderOptions: { store, client } });

    expect(result.indexResult).toEqual({ status: 'cold', tags: ['entity:ticket:index'] });
    expect(result.detailResult).toBeNull();
    expect(client.ticketIndex).not.toHaveBeenCalled();
    expect(client.ticketDetail).not.toHaveBeenCalled();
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { loadTicketDetailRoute } = await importPageLoad(false);

    const result = await loadTicketDetailRoute({ ticketId: 't-1', depends, loaderOptions: { store, client } });

    expect(result.indexResult).toEqual({ status: 'cold', tags: ['entity:ticket:index'] });
    expect(client.ticketIndex).not.toHaveBeenCalled();
  });
});

async function importPageLoad(browser: boolean) {
  return importRouteLoader<typeof import('$lib/routes/loadTicketDetailRoute')>('$lib/routes/loadTicketDetailRoute', browser);
}

function mockClient(overrides: Partial<Record<keyof ReadModelSnapshotClient, ReturnType<typeof vi.fn>>> = {}): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn().mockResolvedValue(ok({} as any)),
    chatDetail: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok({} as any)),
    repoDetail: vi.fn(),
    worktreeDetail: vi.fn(),
    ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot())),
    ticketIndex: vi.fn().mockResolvedValue(ok([])),
    ...overrides
  } as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function ticketSummary(id: string, title: string, workspaceKind = 'unscoped', workspaceId: string | null = null): TicketSummary {
  return {
    id,
    number: null,
    title,
    status: 'waiting',
    workspaceKind: workspaceKind as TicketSummary['workspaceKind'],
    workspaceId,
    repoId: workspaceKind === 'worktree' ? 'repo-1' : null,
    worktreeId: null,
    agentId: null,
    path: null,
    ticketPath: null,
    workspacePath: null,
    errors: [],
    diffStats: null,
    durationSeconds: null,
    chatKey: null,
    runId: null,
    updatedAt: null,
    raw: {}
  };
}

function ticketDetailSnapshot(ticketId = 't-1'): TicketDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'ticket.detail.snapshot',
    cursor: cursor(1, 'ticket.detail'),
    ticket: {
      ticketId,
      routeId: 'TICKET-001',
      title: 'Ticket One',
      status: 'running',
      ownerKind: 'repo',
      ownerId: 'repo-1',
      agent: 'codex',
      model: 'gpt-5.5',
      done: false,
      updatedAt: now
    },
    siblings: [],
    linkedRun: null,
    linkedChats: [],
    artifacts: [],
    dispatchWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    dispatches: [],
    ticketDetail: {},
    ticketQueue: [],
    runQueue: [],
    chatQueue: [],
    repair: repair('/hub/read-models/tickets/t-1')
  };
}

function cursor(sequence: number, source = 'test'): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt: now };
}

function repair(snapshotRoute: string) {
  return {
    snapshotRoute,
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}
