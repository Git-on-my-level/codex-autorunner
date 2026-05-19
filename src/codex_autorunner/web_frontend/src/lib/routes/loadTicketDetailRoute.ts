import {
  ensureTicketDetailLoaded,
  ensureTicketIndexLoaded,
  type ReadModelLoaderResult,
  type TicketOwnerRef
} from '$lib/data';
import { loadReadModelRoute, type LoadReadModelRouteOptions } from './loadReadModelRoute';

export type ScopedTicketDetailLoadData = {
  ticketId: string;
  result: ReadModelLoaderResult;
};

export type RepoTicketDetailLoadData = ScopedTicketDetailLoadData & { repoId: string };
export type WorktreeTicketDetailLoadData = ScopedTicketDetailLoadData & { worktreeId: string };

export type TicketDetailLoadData = {
  ticketId: string;
  indexResult: ReadModelLoaderResult;
  detailResult: ReadModelLoaderResult | null;
};

export type TicketIndexLoadData = {
  result: ReadModelLoaderResult;
};

async function loadScopedTicketDetailRoute(
  owner: TicketOwnerRef,
  ticketId: string,
  options: LoadReadModelRouteOptions
): Promise<ReadModelLoaderResult> {
  return loadReadModelRoute({
    ...options,
    load: (loaderOptions) => ensureTicketDetailLoaded(ticketId, owner, loaderOptions)
  });
}

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadRepoTicketDetailRoute(
  options: LoadReadModelRouteOptions & { repoId?: string; ticketId?: string }
): Promise<RepoTicketDetailLoadData> {
  const repoId = options.repoId ?? '';
  const ticketId = options.ticketId ?? '';
  return {
    repoId,
    ticketId,
    result: await loadScopedTicketDetailRoute({ kind: 'repo', id: repoId }, ticketId, options)
  };
}

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadWorktreeTicketDetailRoute(
  options: LoadReadModelRouteOptions & { worktreeId?: string; ticketId?: string }
): Promise<WorktreeTicketDetailLoadData> {
  const worktreeId = options.worktreeId ?? '';
  const ticketId = options.ticketId ?? '';
  return {
    worktreeId,
    ticketId,
    result: await loadScopedTicketDetailRoute({ kind: 'worktree', id: worktreeId }, ticketId, options)
  };
}

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadTicketIndexRoute(options: LoadReadModelRouteOptions): Promise<TicketIndexLoadData> {
  return {
    result: await loadReadModelRoute({
      ...options,
      load: (loaderOptions) => ensureTicketIndexLoaded(loaderOptions)
    })
  };
}

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadTicketDetailRoute(
  options: LoadReadModelRouteOptions & { ticketId?: string }
): Promise<TicketDetailLoadData> {
  const ticketId = options.ticketId ?? '';
  const indexResult = await loadReadModelRoute({
    ...options,
    load: (loaderOptions) => ensureTicketIndexLoaded(loaderOptions)
  });

  const store = options.loaderOptions?.store;
  let detailResult: ReadModelLoaderResult | null = null;
  if (store && ticketId) {
    const state = store.snapshot();
    const summaryIds = state.ticketOrderByOwner['all'];
    const summaries = summaryIds?.map((id) => state.ticketSummaries[id]).filter(Boolean) ?? [];
    const matched = summaries.find(
      (summary) =>
        summary.id === ticketId &&
        (summary.workspaceKind === 'repo' || summary.workspaceKind === 'worktree') &&
        summary.workspaceId
    );
    if (
      matched?.workspaceId &&
      (matched.workspaceKind === 'repo' || matched.workspaceKind === 'worktree')
    ) {
      detailResult = await loadScopedTicketDetailRoute(
        { kind: matched.workspaceKind, id: matched.workspaceId },
        ticketId,
        { loaderOptions: options.loaderOptions, depends: undefined }
      );
    }
  }

  return { ticketId, indexResult, detailResult };
}
