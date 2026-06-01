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

const now = '2026-05-17T12:00:00Z';

describe('/repos/[repoId]/tickets route load', () => {
  it('fetches and hydrates repo detail when blocking', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const client = mockClient({
      repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot('repo-1')))
    });
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({
      repoId: 'repo-1',
      depends,
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.repoId).toBe('repo-1');
    expect(result.result.status).toBe('fetched');
    expect(depends).toHaveBeenCalledWith('entity:repo:repo-1');
    expect(store.snapshot().repoDetails['repo-1']).toBeDefined();
  });

  it('returns cold without blocking when repo detail is missing', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot('repo-1')))
    });
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({
      repoId: 'repo-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:repo:repo-1'] });
    expect(client.repoDetail).not.toHaveBeenCalled();
  });

  it('returns cache hit when repo detail is already hydrated', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const client = mockClient();
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({
      repoId: 'repo-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({ status: 'cache-hit', tags: ['entity:repo:repo-1'] });
    expect(client.repoDetail).toHaveBeenCalledWith('repo-1');
  });

  it('returns errors from blocking repo detail fetches', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Not found');
    const client = mockClient({ repoDetail: vi.fn().mockResolvedValue(fail(error)) });
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({
      repoId: 'repo-1',
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.result).toEqual({ status: 'error', tags: ['entity:repo:repo-1'], error });
  });
});

async function importPageLoad(browser: boolean) {
  return importRouteLoader<typeof import('$lib/routes/loadRepoDetailRoute')>('$lib/routes/loadRepoDetailRoute', browser);
}

function mockClient(overrides: Partial<Record<keyof ReadModelSnapshotClient, ReturnType<typeof vi.fn>>> = {}): ReadModelSnapshotClient {
  return {
    chatIndex: vi.fn().mockResolvedValue(ok({} as any)),
    chatDetail: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok({} as any)),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok({} as any)),
    repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot())),
    worktreeDetail: vi.fn().mockResolvedValue(ok({} as any)),
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

function repoDetailSnapshot(ownerId = 'repo-1'): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, 'repo.detail'),
    ownerKind: 'repo',
    ownerId,
    identity: {},
    parentLinks: {},
    topology: {},
    runtime: {},
    ticketQueue: [],
    runQueue: [],
    chatQueue: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: pageWindow(),
    runWindow: pageWindow(),
    chatWindow: pageWindow(),
    artifactWindow: pageWindow(),
    repair: repair('/hub/read-models/repos/repo-1')
  };
}

function cursor(sequence: number, source = 'test'): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt: now };
}

function pageWindow() {
  return { limit: 20, totalEstimate: 0, totalIsExact: true };
}

function repair(snapshotRoute: string) {
  return {
    snapshotRoute,
    cursorQueryParam: 'after' as const,
    gapEventType: 'projection.cursor_gap' as const,
    behavior: 'repair_snapshot_required' as const
  };
}
