import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';

const now = '2026-05-11T12:00:00Z';

describe('/repos/[repoId] route load', () => {
  it('returns a cache hit when the repo detail is already in the store', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const client = mockClient();
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({ repoId: 'repo-1', loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cache-hit', tags: ['entity:repo:repo-1'] });
    expect(client.repoDetail).not.toHaveBeenCalled();
  });

  it('fetches and hydrates a missing repo detail', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot('repo-2')))
    });
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({ repoId: 'repo-2', loaderOptions: { store, client } });

    expect(result.result.status).toBe('fetched');
    expect(client.repoDetail).toHaveBeenCalledWith('repo-2');
    expect(store.snapshot().repoDetails['repo-2']).toBeDefined();
  });

  it('registers the repo entity dependency', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const depends = vi.fn();
    const { loadRepoDetailRoute } = await importPageLoad(true);

    await loadRepoDetailRoute({ repoId: 'repo-1', depends, loaderOptions: { store, client: mockClient() } });

    expect(depends).toHaveBeenCalledWith('entity:repo:repo-1');
  });

  it('returns an error handle when repo detail fetch fails', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot unavailable');
    const client = mockClient({
      repoDetail: vi.fn().mockResolvedValue(fail(error))
    });
    const { loadRepoDetailRoute } = await importPageLoad(true);

    const result = await loadRepoDetailRoute({ repoId: 'repo-1', loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'error', tags: ['entity:repo:repo-1'], error });
    expect(store.snapshot().repoDetails['repo-1']).toBeUndefined();
  });

  it('returns cold when not in the browser', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    const depends = vi.fn();
    const { loadRepoDetailRoute } = await importPageLoad(false);

    const result = await loadRepoDetailRoute({ repoId: 'repo-1', depends, loaderOptions: { store, client } });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:repo:repo-1'] });
    expect(depends).toHaveBeenCalledWith('entity:repo:repo-1');
    expect(client.repoDetail).not.toHaveBeenCalled();
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
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok(repoWorktreeTopologySnapshot())),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(repoWorktreeRuntimeSnapshot())),
    repoDetail: vi.fn().mockResolvedValue(ok(repoDetailSnapshot())),
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

function repoDetailSnapshot(repoId = 'repo-1'): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, 'repo.detail'),
    ownerKind: 'repo',
    ownerId: repoId,
    identity: { id: repoId, name: 'Test Repo', path: '/repo', kind: 'base', worktree_count: 0 },
    parentLinks: {},
    topology: { children: [] },
    runtime: {},
    scopedTickets: [],
    scopedRuns: [],
    scopedChats: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    runWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    chatWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    artifactWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    repair: repair(`/hub/read-models/repos/${repoId}`)
  };
}

function worktreeDetailSnapshot(worktreeId = 'wt-1'): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(2, 'worktree.detail'),
    ownerKind: 'worktree',
    ownerId: worktreeId,
    identity: { id: worktreeId, name: 'Test Worktree', path: '/repo/wt', kind: 'worktree', worktree_of: 'repo-1' },
    parentLinks: {},
    topology: {},
    runtime: {},
    scopedTickets: [],
    scopedRuns: [],
    scopedChats: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    runWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    chatWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    artifactWindow: { limit: 20, totalEstimate: 0, totalIsExact: true },
    repair: repair(`/hub/read-models/worktrees/${worktreeId}`)
  };
}

function repoWorktreeTopologySnapshot(): RepoWorktreeTopologySnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.topology.snapshot',
    cursor: cursor(3, 'repo_worktree.topology'),
    window: { limit: 200, totalEstimate: 0, totalIsExact: true },
    repos: [],
    worktrees: [],
    repair: repair('/hub/read-models/repo-worktree/topology')
  };
}

function repoWorktreeRuntimeSnapshot(): RepoWorktreeRuntimeSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.runtime.snapshot',
    cursor: cursor(4, 'repo_worktree.runtime'),
    window: { limit: 200, totalEstimate: 0, totalIsExact: true },
    runtime: [],
    repair: repair('/hub/read-models/repo-worktree/runtime')
  };
}
