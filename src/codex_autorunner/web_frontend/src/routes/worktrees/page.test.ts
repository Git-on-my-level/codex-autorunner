import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ProjectionCursor,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot
} from '$lib/api/readModelContracts';
import { readModelEntityStore } from '$lib/data';
import Page from './+page.svelte';

const now = '2026-05-11T12:00:00Z';

describe('/worktrees index page', () => {
  afterEach(() => {
    readModelEntityStore.reset();
  });

  it('keeps cached rows visible when a background refresh has failed', () => {
    readModelEntityStore.applyRepoWorktreeTopologySnapshot(topologySnapshot());
    readModelEntityStore.applyRepoWorktreeRuntimeSnapshot(runtimeSnapshot());

    const { body } = render(Page, {
      props: {
        data: {
          status: 'error',
          tags: [],
          error: { kind: 'http', status: 503, code: 'http_503', message: 'Hub restarting' }
        }
      }
    });

    expect(body).toContain('Worktree One');
    expect(body).not.toContain('Could not load workspace state');
  });
});

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
