import { describe, expect, it, vi } from 'vitest';
import type { ApiError } from '$lib/api/client';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeDetailSnapshot,
  type RepoWorktreeTopologySnapshot
} from '$lib/api/readModelContracts';
import { ReadModelEntityStore } from '$lib/data/readModelStore';
import {
  createRepoWorktreeDetailSession,
  type RepoWorktreeDetailSessionDependencies
} from './repoWorktreeDetailSession';

const now = '2026-05-18T12:00:00Z';

describe('repo worktree detail session', () => {
  it('hydrates a cached repo snapshot into a detail view model', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const session = createSession('repo', 'repo-1', { store });

    await session.hydrate();

    expect(session.state.loading).toBe(false);
    expect(session.state.error).toBeNull();
    expect(session.state.detail).toMatchObject({ kind: 'repo', id: 'repo-1', title: 'Repo repo-1' });
  });

  it('cold fetches and hydrates a missing repo snapshot', async () => {
    const store = new ReadModelEntityStore();
    const loadRepoDetail = vi.fn(async (repoId: string) => {
      store.applyRepoDetailSnapshot(repoDetailSnapshot(repoId));
      return { status: 'fetched' as const, tags: [`entity:repo:${repoId}`] };
    });
    const session = createSession('repo', 'repo-2', { store, loadRepoDetail });

    await session.hydrate();

    expect(loadRepoDetail).toHaveBeenCalledWith('repo-2', { refresh: true });
    expect(session.state.detail?.id).toBe('repo-2');
    expect(session.state.loading).toBe(false);
  });

  it('surfaces failed fetches without hydrating stale detail', async () => {
    const store = new ReadModelEntityStore();
    const error = apiError('Snapshot failed');
    const session = createSession('repo', 'repo-1', {
      store,
      loadRepoDetail: vi.fn(async () => ({ status: 'error' as const, tags: ['entity:repo:repo-1'], error }))
    });

    await session.hydrate();

    expect(session.state.error).toBe(error);
    expect(session.state.detail).toBeNull();
    expect(session.state.loading).toBe(false);
  });

  it('invalidates repo tags and quietly reloads after repo sync succeeds', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const invalidateTags = vi.fn(async () => undefined);
    const loadRepoDetail = vi.fn(async (repoId: string) => {
      store.applyRepoDetailSnapshot(repoDetailSnapshot(repoId));
      return { status: 'fetched' as const, tags: [`entity:repo:${repoId}`] };
    });
    const session = createSession('repo', 'repo-1', {
      store,
      loadRepoDetail,
      invalidateTags,
      syncRepoMain: vi.fn(async () => ({ ok: true as const }))
    });

    await session.hydrate();
    await session.syncRepo();

    expect(invalidateTags).toHaveBeenCalledWith(['entity:repo-worktree:index', 'entity:repo:repo-1']);
    expect(loadRepoDetail).toHaveBeenCalledWith('repo-1', { refresh: true });
    expect(session.state.notice).toEqual({ tone: 'success', message: 'Synced default branch with origin.' });
  });

  it('resolves a worktree parent repo for sync and invalidates both owner tags', async () => {
    const store = new ReadModelEntityStore();
    store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot('wt-1', 'repo-1'));
    const invalidateTags = vi.fn(async () => undefined);
    const session = createSession('worktree', 'wt-1', {
      store,
      invalidateTags,
      syncRepoMain: vi.fn(async () => ({ ok: true as const })),
      loadWorktreeDetail: vi.fn(async (worktreeId: string) => {
        store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot(worktreeId, 'repo-1'));
        return { status: 'fetched' as const, tags: [`entity:worktree:${worktreeId}`] };
      })
    });

    await session.hydrate();
    await session.syncRepo();

    expect(session.state.backingRepoId).toBe('repo-1');
    expect(invalidateTags).toHaveBeenCalledWith([
      'entity:repo-worktree:index',
      'entity:repo:repo-1',
      'entity:worktree:wt-1'
    ]);
  });

  it('redirects legacy worktree paths when the parent repo can be resolved', async () => {
    const store = new ReadModelEntityStore();
    store.applyWorktreeDetailSnapshot(worktreeDetailSnapshot('wt-1', 'repo-1'));
    const redirect = vi.fn(async () => undefined);
    const session = createSession('worktree', 'wt-1', {
      store,
      currentPath: () => '/worktrees/wt-1',
      redirect
    });

    await session.hydrate();

    expect(redirect).toHaveBeenCalledWith('/repos/repo-1/worktrees/wt-1');
    expect(session.state.detail).toBeNull();
  });

  it('reloads after successful retire actions', async () => {
    const store = new ReadModelEntityStore();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    const loadRepoDetail = vi.fn(async (repoId: string) => {
      store.applyRepoDetailSnapshot(repoDetailSnapshot(repoId));
      return { status: 'fetched' as const, tags: [`entity:repo:${repoId}`] };
    });
    const session = createSession('repo', 'repo-1', {
      store,
      loadRepoDetail,
      retireWorktree: vi.fn(async () => ({ tone: 'success' as const, message: 'retired' })),
      retireState: vi.fn(async () => ({ tone: 'success' as const, message: 'retired' }))
    });

    await session.retireWorktree({
      id: 'wt-1',
      label: 'Worktree',
      chatBound: false,
      cleanupBlockedByChatBinding: false
    });
    await session.retireState({
      kind: 'repo',
      id: 'repo-1',
      label: 'Repo',
      hasCarState: true,
      unboundManagedThreadCount: 0
    });

    expect(loadRepoDetail).toHaveBeenCalledTimes(2);
    expect(session.state.notice).toEqual({ tone: 'success', message: 'retired' });
  });

  it('ignores stale async responses after the route owner changes', async () => {
    const store = new ReadModelEntityStore();
    const firstLoad = deferred<{ status: 'fetched'; tags: string[] }>();
    const loadRepoDetail = vi.fn((repoId: string) => {
      if (repoId === 'repo-1') return firstLoad.promise;
      store.applyRepoDetailSnapshot(repoDetailSnapshot(repoId));
      return Promise.resolve({ status: 'fetched' as const, tags: [`entity:repo:${repoId}`] });
    });
    const session = createSession('repo', 'repo-1', { store, loadRepoDetail });

    const staleLoad = session.load();
    session.setOwner('repo', 'repo-2', { status: 'cold', tags: ['entity:repo:repo-2'] });
    await session.load();
    store.applyRepoDetailSnapshot(repoDetailSnapshot('repo-1'));
    firstLoad.resolve({ status: 'fetched', tags: ['entity:repo:repo-1'] });
    await staleLoad;

    expect(session.state.ownerId).toBe('repo-2');
    expect(session.state.detail?.id).toBe('repo-2');
  });

  it('ignores stale provisional topology after the route owner changes during hydrate', async () => {
    const store = new ReadModelEntityStore();
    const topologyLoad = deferred<{ status: 'fetched'; tags: string[] }>();
    const session = createSession('repo', 'repo-1', {
      store,
      loadRepoWorktreeIndex: vi.fn(() => topologyLoad.promise),
      loadRepoDetail: vi.fn(async (repoId: string) => {
        store.applyRepoDetailSnapshot(repoDetailSnapshot(repoId));
        return { status: 'fetched' as const, tags: [`entity:repo:${repoId}`] };
      })
    });

    const staleHydrate = session.hydrate();
    session.setOwner('repo', 'repo-2', { status: 'cold', tags: ['entity:repo:repo-2'] });
    await session.load();
    store.applyRepoWorktreeTopologySnapshot(repoTopologySnapshot('repo-1'));
    topologyLoad.resolve({ status: 'fetched', tags: ['entity:repo-worktree:index'] });
    await staleHydrate;

    expect(session.state.ownerId).toBe('repo-2');
    expect(session.state.detail?.id).toBe('repo-2');
  });
});

function createSession(
  ownerKind: 'repo' | 'worktree',
  ownerId: string,
  overrides: Partial<RepoWorktreeDetailSessionDependencies> = {}
) {
  return createRepoWorktreeDetailSession({
    ownerKind,
    ownerId,
    loaderResult: { status: 'cold', tags: [] },
    dependencies: {
      store: new ReadModelEntityStore(),
      loadRepoWorktreeIndex: vi.fn(async () => ({ status: 'cold' as const, tags: [] })),
      loadRepoDetail: vi.fn(async () => ({ status: 'cold' as const, tags: [] })),
      loadWorktreeDetail: vi.fn(async () => ({ status: 'cold' as const, tags: [] })),
      syncRepoMain: vi.fn(async () => ({ ok: true as const })),
      invalidateTags: vi.fn(async () => undefined),
      retireWorktree: vi.fn(async () => null),
      retireState: vi.fn(async () => null),
      ...overrides
    }
  });
}

function repoDetailSnapshot(repoId: string): RepoWorktreeDetailSnapshot {
  return detailSnapshot({
    ownerKind: 'repo',
    ownerId: repoId,
    identity: { id: repoId, name: `Repo ${repoId}`, path: `/repos/${repoId}`, kind: 'base', worktree_count: 0 },
    topology: { children: [] }
  });
}

function repoTopologySnapshot(repoId: string): RepoWorktreeTopologySnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.topology.snapshot',
    cursor: cursor(1, 'repo_worktree.topology'),
    window: window(),
    repos: [
      {
        repoId,
        label: `Repo ${repoId}`,
        path: `/repos/${repoId}`,
        archived: false,
        isPinned: false,
        destinationId: null,
        childWorktreeIds: [],
        worktreeSetupCommands: null,
        chatBound: false,
        chatBindingCount: 0,
        chatBindingSources: {},
        chatBindingDisplayNames: []
      }
    ],
    worktrees: [],
    repair: {
      snapshotRoute: '/hub/read-models/repo-worktree/topology',
      cursorQueryParam: 'after',
      gapEventType: 'projection.cursor_gap',
      behavior: 'repair_snapshot_required'
    }
  };
}

function worktreeDetailSnapshot(worktreeId: string, repoId: string | null): RepoWorktreeDetailSnapshot {
  return detailSnapshot({
    ownerKind: 'worktree',
    ownerId: worktreeId,
    identity: {
      id: worktreeId,
      name: `Worktree ${worktreeId}`,
      path: `/repos/${repoId ?? 'unknown'}/${worktreeId}`,
      kind: 'worktree',
      worktree_of: repoId
    },
    topology: {}
  });
}

function detailSnapshot(input: {
  ownerKind: 'repo' | 'worktree';
  ownerId: string;
  identity: Record<string, unknown>;
  topology: Record<string, unknown>;
}): RepoWorktreeDetailSnapshot {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    kind: 'repo_worktree.detail.snapshot',
    cursor: cursor(1, `${input.ownerKind}.detail`),
    ownerKind: input.ownerKind,
    ownerId: input.ownerId,
    identity: input.identity,
    parentLinks: {},
    topology: input.topology,
    runtime: {},
    ticketQueue: [],
    runQueue: [],
    chatQueue: [],
    contextspaceSummary: [],
    currentArtifacts: [],
    ticketWindow: window(),
    runWindow: window(),
    chatWindow: window(),
    artifactWindow: window(),
    repair: {
      snapshotRoute: `/hub/read-models/${input.ownerKind}s/${input.ownerId}`,
      cursorQueryParam: 'after',
      gapEventType: 'projection.cursor_gap',
      behavior: 'repair_snapshot_required'
    }
  };
}

function cursor(sequence: number, source: string): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt: now };
}

function window() {
  return { limit: 20, totalEstimate: 0, totalIsExact: true };
}

function apiError(message: string): ApiError {
  return { kind: 'http', status: 503, code: 'unavailable', message };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
