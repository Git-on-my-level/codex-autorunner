import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';
import { importRouteLoader } from '$lib/test/importRouteLoader';

const now = '2026-05-11T12:00:00Z';

describe('/worktrees/[worktreeId] route load', () => {
  it('returns a cache hit when the worktree detail is already in the store', async () => {
    const store = new ReadModelEntityStore();
    store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot('wt-1'));
    const client = mockClient();
    const { loadWorktreeDetailRoute } = await importPageLoad(true);

    const result = await loadWorktreeDetailRoute({ worktreeId: 'wt-1', loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cache-hit', tags: ['entity:worktree:wt-1'] });
    expect(client.worktreeDetail).not.toHaveBeenCalled();
  });

  it('returns cold on a missing worktree detail so navigation can commit immediately', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot('wt-2')))
    });
    const { loadWorktreeDetailRoute } = await importPageLoad(true);

    const result = await loadWorktreeDetailRoute({ worktreeId: 'wt-2', loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:worktree:wt-2'] });
    expect(client.worktreeDetail).not.toHaveBeenCalled();
    expect(store.snapshot().worktreeDetails['wt-2']).toBeUndefined();
  });

  it('can fetch and hydrate a missing worktree detail when explicitly blocking', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot('wt-2')))
    });
    const { loadWorktreeDetailRoute } = await importPageLoad(true);

    const result = await loadWorktreeDetailRoute({ worktreeId: 'wt-2', loaderOptions: { store, client, blocking: true } });

    expect(result.result.status).toBe('fetched');
    expect(client.worktreeDetail).toHaveBeenCalledWith('wt-2');
    expect(store.snapshot().worktreeDetails['wt-2']).toBeDefined();
  });

  it('registers the worktree entity dependency', async () => {
    const store = new ReadModelEntityStore();
    store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot('wt-1'));
    const depends = vi.fn();
    const { loadWorktreeDetailRoute } = await importPageLoad(true);

    await loadWorktreeDetailRoute({ worktreeId: 'wt-1', depends, loaderOptions: { store, client: mockClient() } });

    expect(depends).toHaveBeenCalledWith('entity:worktree:wt-1');
  });

  it('returns an error handle when blocking worktree detail fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot unavailable');
    const client = mockClient({
      worktreeDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { loadWorktreeDetailRoute } = await importPageLoad(true);

    const result = await loadWorktreeDetailRoute({ worktreeId: 'wt-1', loaderOptions: { store, client, blocking: true } });

    expect(result.result).toEqual({ status: 'error', tags: ['entity:worktree:wt-1'], error });
    expect(store.snapshot().worktreeDetails['wt-1']).toBeUndefined();
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { loadWorktreeDetailRoute } = await importPageLoad(false);

    const result = await loadWorktreeDetailRoute({ worktreeId: 'wt-1', depends, loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:worktree:wt-1'] });
    expect(depends).toHaveBeenCalledWith('entity:worktree:wt-1');
    expect(client.worktreeDetail).not.toHaveBeenCalled();
  });
});

async function importPageLoad(browser: boolean) {
  return importRouteLoader<typeof import('$lib/routes/loadWorktreeDetailRoute')>('$lib/routes/loadWorktreeDetailRoute', browser);
}

function mockClient(overrides: Partial<Record<keyof ReadModelSnapshotClient, ReturnType<typeof vi.fn>>> = {}): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn().mockResolvedValue(ok({} as any)),
    chatDetail: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok({} as any)),
    repoDetail: vi.fn().mockResolvedValue(ok({} as any)),
    worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot())),
    ticketDetail: vi.fn().mockResolvedValue(ok({} as any)),
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
  return { kind: 'http', status: 503, code: 'unavailable', message };
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

function worktreeDetailSnapshot(worktreeId = 'wt-1'): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, 'worktree.detail'),
    ownerKind: 'worktree',
    ownerId: worktreeId,
    identity: { id: worktreeId, name: 'Test Worktree', path: '/repo/wt', kind: 'worktree', worktree_of: 'repo-1' },
    parentLinks: {},
    topology: {},
    runtime: {},
    ticketQueue: [],
    runQueue: [],
    chatQueue: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    runWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    chatWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    artifactWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    repair: repair(`/hub/read-models/worktrees/${worktreeId}`)
  };
}
