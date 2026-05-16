import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type TicketDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';

const now = '2026-05-11T12:00:00Z';

describe('/repos/[repoId]/tickets/[ticketId] route load', () => {
  it('fetches and hydrates repo-scoped ticket detail', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const client = mockClient({
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot('t-1', 'repo-1')))
    });
    const { loadRepoTicketDetailRoute } = await importPageLoad(true);

    const result = await loadRepoTicketDetailRoute({
      repoId: 'repo-1',
      ticketId: 't-1',
      depends,
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.repoId).toBe('repo-1');
    expect(result.ticketId).toBe('t-1');
    expect(result.result.status).toBe('fetched');
    expect(depends).toHaveBeenCalledWith('entity:ticket:t-1');
    expect(depends).toHaveBeenCalledWith('entity:repo:repo-1');
    expect(store.snapshot().tickets['t-1']?.title).toBe('Ticket One');
  });

  it('returns cache hit when ticket detail is already hydrated', async () => {
    const store = new ReadModelEntityStore();
    store.applyTicketDetailSnapshot(ticketDetailSnapshot('t-1', 'repo-1'));
    const client = mockClient();
    const { loadRepoTicketDetailRoute } = await importPageLoad(true);

    const result = await loadRepoTicketDetailRoute({
      repoId: 'repo-1',
      ticketId: 't-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({
      status: 'cache-hit',
      tags: ['entity:ticket:t-1', 'entity:repo:repo-1']
    });
    expect(client.ticketDetail).not.toHaveBeenCalled();
  });

  it('returns cold without blocking when ticket detail is missing', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      ticketDetail: vi.fn().mockResolvedValue(ok(ticketDetailSnapshot('t-1', 'repo-1')))
    });
    const { loadRepoTicketDetailRoute } = await importPageLoad(true);

    const result = await loadRepoTicketDetailRoute({
      repoId: 'repo-1',
      ticketId: 't-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({
      status: 'cold',
      tags: ['entity:ticket:t-1', 'entity:repo:repo-1']
    });
    expect(client.ticketDetail).not.toHaveBeenCalled();
  });

  it('returns error when blocking ticket detail fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Not found');
    const client = mockClient({
      ticketDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { loadRepoTicketDetailRoute } = await importPageLoad(true);

    const result = await loadRepoTicketDetailRoute({
      repoId: 'repo-1',
      ticketId: 't-1',
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.result).toEqual({
      status: 'error',
      tags: ['entity:ticket:t-1', 'entity:repo:repo-1'],
      error
    });
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { loadRepoTicketDetailRoute } = await importPageLoad(false);

    const result = await loadRepoTicketDetailRoute({
      repoId: 'repo-1',
      ticketId: 't-1',
      depends,
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({
      status: 'cold',
      tags: ['entity:ticket:t-1', 'entity:repo:repo-1']
    });
    expect(client.ticketDetail).not.toHaveBeenCalled();
  });
});

async function importPageLoad(browser: boolean) {
  vi.resetModules();
  vi.doMock('$app/environment', () => ({ browser, dev: false, building: false, version: 'test' }));
  return import('./+page');
}

function mockClient(overrides: Partial<Record<keyof ReadModelSnapshotClient, ReturnType<typeof vi.fn>>> = {}): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn().mockResolvedValue(ok({} as any)),
    chatDetail: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok({} as any)),
    repoDetail: vi.fn(),
    worktreeDetail: vi.fn(),
    ticketDetail: vi.fn().mockResolvedValue(ok({} as any)),
    ticketIndex: vi.fn().mockResolvedValue(ok([])),
    ...overrides
  } as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: ApiError): ApiResult<T> {
  return { ok: false, error };
}

function apiError(message: string): ApiError {
  return { kind: 'http', status: 404, code: 'not_found', message };
}

function ticketDetailSnapshot(ticketId = 't-1', ownerId = 'repo-1'): TicketDetailSnapshot {
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
    dispatchWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    dispatches: [],
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
