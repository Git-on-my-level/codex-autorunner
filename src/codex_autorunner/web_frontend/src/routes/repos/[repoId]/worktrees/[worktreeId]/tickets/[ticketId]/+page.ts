import {
  ensureTicketDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type RepoWorktreeTicketDetailLoadData = {
  worktreeId: string;
  ticketId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<RepoWorktreeTicketDetailLoadData> => {
  return loadRepoWorktreeTicketDetailRoute({ worktreeId: params.worktreeId, ticketId: params.ticketId, depends });
};

export async function loadRepoWorktreeTicketDetailRoute(options: {
  worktreeId?: string;
  ticketId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<RepoWorktreeTicketDetailLoadData> {
  const worktreeId = options.worktreeId ?? '';
  const ticketId = options.ticketId ?? '';
  return {
    worktreeId,
    ticketId,
    result: await ensureTicketDetailLoaded(ticketId, { kind: 'worktree', id: worktreeId }, {
      ...options.loaderOptions,
      depends: options.depends,
      blocking: options.loaderOptions?.blocking ?? false
    })
  };
}
