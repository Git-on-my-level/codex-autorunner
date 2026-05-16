import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot
} from '$lib/api/readModelContracts';
import type { ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';

const now = '2026-05-11T12:00:00Z';

describe('/worktrees index route load', () => {
  it('returns a cache hit when the index is already in the store', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoWorktreeTopologySnapshot(topologySnapshot());
    store.applyRepoWorktreeRuntimeSnapshot(runtimeSnapshot());
    const client = mockClient();
    const depends = vi.fn();
    vi.doMock('$lib/data/readModelStore', () => ({ readModelEntityStore: store }));
    vi.doMock('$lib/data/readModelClients', () => ({ readModelSnapshotClient: client, createReadModelSnapshotClient: () => client }));
    const { load } = await importPageLoad(true);

    const result = await load({ depends } as any) as any;

    expect(result.status).toBe('cache-hit');
    expect(result.tags).toContain('entity:repo-worktree:index');
    expect(depends).toHaveBeenCalledWith('entity:repo-worktree:index');
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
  });

  it('returns cold when the store is empty so navigation can commit immediately', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient();
    vi.doMock('$lib/data/readModelStore', () => ({ readModelEntityStore: store }));
    vi.doMock('$lib/data/readModelClients', () => ({ readModelSnapshotClient: client, createReadModelSnapshotClient: () => client }));
    const { load } = await importPageLoad(true);

    const result = await load({ depends: vi.fn() } as any) as any;

    expect(result.status).toBe('cold');
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
    expect(client.repoWorktreeRuntime).not.toHaveBeenCalled();
  });

  it('returns cold when not in the browser', async () => {
    const client = mockClient();
    const depends = vi.fn();
    const { load } = await importPageLoad(false);

    const result = await load({ depends } as any) as any;

    expect(result.status).toBe('cold');
    expect(client.repoWorktreeTopology).not.toHaveBeenCalled();
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
    repoWorktreeTopology: vi.fn().mockResolvedValue(ok(topologySnapshot())),
    repoWorktreeRuntime: vi.fn().mockResolvedValue(ok(runtimeSnapshot())),
    repoDetail: vi.fn().mockResolvedValue(ok({} as any)),
    worktreeDetail: vi.fn().mockResolvedValue(ok({} as any)),
    ticketDetail: vi.fn().mockResolvedValue(ok({} as any)),
    ...overrides
  } as ReadModelSnapshotClient;
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
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

function topologySnapshot(): RepoWorktreeTopologySnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.topology.snapshot',
    cursor: cursor(1, 'repo_worktree.topology'),
    window: { limit: 200, totalEstimate: 1, totalIsExact: true },
    repos: [{ repoId: 'repo-1', label: 'Repo One', path: '/repo', archived: false, childWorktreeIds: ['wt-1'] }],
    worktrees: [{ worktreeId: 'wt-1', repoId: 'repo-1', label: 'Worktree One', path: '/repo/wt', branch: 'main', archived: false }],
    repair: repair('/hub/read-models/repo-worktree/topology')
  };
}

function runtimeSnapshot(): RepoWorktreeRuntimeSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.runtime.snapshot',
    cursor: cursor(2, 'repo_worktree.runtime'),
    window: { limit: 200, totalEstimate: 0, totalIsExact: true },
    runtime: [],
    repair: repair('/hub/read-models/repo-worktree/runtime')
  };
}
