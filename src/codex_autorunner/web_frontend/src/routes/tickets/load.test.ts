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

describe('/tickets route load', () => {
  it('returns cold without blocking navigation when the ticket index is missing', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const summaries = [ticketSummary('t-1', 'Ticket A')];
    const client = mockClient({
      ticketIndex: vi.fn().mockResolvedValue(ok(summaries))
    });
    const { loadTicketIndexRoute } = await importPageLoad(true);

    const result = await loadTicketIndexRoute({ depends, loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:ticket:index'] });
    expect(depends).toHaveBeenCalledWith('entity:ticket:index');
    expect(client.ticketIndex).not.toHaveBeenCalled();
    expect(store.snapshot().ticketOrderByOwner['all']).toBeUndefined();
  });

  it('can fetch and return ticket index result when explicitly blocking', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const summaries = [ticketSummary('t-1', 'Ticket A')];
    const client = mockClient({
      ticketIndex: vi.fn().mockResolvedValue(ok(summaries))
    });
    const { loadTicketIndexRoute } = await importPageLoad(true);

    const result = await loadTicketIndexRoute({ depends, loaderOptions: { store, client, blocking: true } });

    expect(result.result.status).toBe('fetched');
    expect(depends).toHaveBeenCalledWith('entity:ticket:index');
    expect(store.snapshot().ticketOrderByOwner['all']).toEqual(['t-1']);
  });

  it('returns cache hit on revisit', async () => {
    const store = new ReadModelEntityStore();
    store.replaceScopedTicketSummaries('all', [ticketSummary('t-1', 'Cached')]);
    const client = mockClient();
    const { loadTicketIndexRoute } = await importPageLoad(true);

    const result = await loadTicketIndexRoute({ loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cache-hit', tags: ['entity:ticket:index'] });
    expect(client.ticketIndex).toHaveBeenCalled();
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { loadTicketIndexRoute } = await importPageLoad(false);

    const result = await loadTicketIndexRoute({ depends, loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:ticket:index'] });
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
    ticketDetail: vi.fn().mockResolvedValue(ok({} as any)),
    ticketIndex: vi.fn().mockResolvedValue(ok([])),
    ...overrides
  } as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function ticketSummary(id: string, title: string): TicketSummary {
  return {
    id,
    number: null,
    title,
    status: 'waiting',
    workspaceKind: 'unscoped',
    workspaceId: null,
    repoId: null,
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
