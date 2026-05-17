import { describe, expect, it, vi } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot
} from '$lib/api/readModelContracts';
import type { ApiError, ApiResult } from '$lib/api/client';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import type { ReadModelSnapshotClient } from '$lib/data/readModelClients';

const now = '2026-05-17T12:00:00Z';

describe('/worktrees/[worktreeId]/tickets route load', () => {
  it('fetches and hydrates worktree detail when blocking', async () => {
    const store = new ReadModelEntityStore();
    const depends = vi.fn();
    const client = mockClient({
      worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot('wt-1')))
    });
    const { loadWorktreeTicketListRoute } = await importPageLoad(true);

    const result = await loadWorktreeTicketListRoute({
      worktreeId: 'wt-1',
      depends,
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.worktreeId).toBe('wt-1');
    expect(result.result.status).toBe('fetched');
    expect(depends).toHaveBeenCalledWith('entity:worktree:wt-1');
    expect(store.snapshot().worktreeDetails['wt-1']).toBeDefined();
  });

  it('returns cold without blocking when worktree detail is missing', async () => {
    const store = new ReadModelEntityStore();
    const client = mockClient({
      worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot('wt-1')))
    });
    const { loadWorktreeTicketListRoute } = await importPageLoad(true);

    const result = await loadWorktreeTicketListRoute({
      worktreeId: 'wt-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({ status: 'cold', tags: ['entity:worktree:wt-1'] });
    expect(client.worktreeDetail).not.toHaveBeenCalled();
  });

  it('returns cache hit when worktree detail is already hydrated', async () => {
    const store = new ReadModelEntityStore();
    store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot('wt-1'));
    const client = mockClient();
    const { loadWorktreeTicketListRoute } = await importPageLoad(true);

    const result = await loadWorktreeTicketListRoute({
      worktreeId: 'wt-1',
      loaderOptions: { store, client }
    });

    expect(result.result).toEqual({ status: 'cache-hit', tags: ['entity:worktree:wt-1'] });
    expect(client.worktreeDetail).not.toHaveBeenCalled();
  });

  it('returns errors from blocking worktree detail fetches', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Not found');
    const client = mockClient({ worktreeDetail: vi.fn().mockResolvedValue(fail(error)) });
    const { loadWorktreeTicketListRoute } = await importPageLoad(true);

    const result = await loadWorktreeTicketListRoute({
      worktreeId: 'wt-1',
      loaderOptions: { store, client, blocking: true }
    });

    expect(result.result).toEqual({ status: 'error', tags: ['entity:worktree:wt-1'], error });
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
    repoDetail: vi.fn().mockResolvedValue(ok({} as any)),
    worktreeDetail: vi.fn().mockResolvedValue(ok(worktreeDetailSnapshot())),
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

function worktreeDetailSnapshot(ownerId = 'wt-1'): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, 'worktree.detail'),
    ownerKind: 'worktree',
    ownerId,
    identity: {},
    parentLinks: { repo_id: 'repo-1' },
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
    repair: repair('/hub/read-models/worktrees/wt-1')
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
