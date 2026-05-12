import {
  ensureTicketDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type WorktreeTicketDetailLoadData = {
  worktreeId: string;
  ticketId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeTicketDetailLoadData> => {
  return loadWorktreeTicketDetailRoute({ worktreeId: params.worktreeId, ticketId: params.ticketId, depends });
};

export async function loadWorktreeTicketDetailRoute(options: {
  worktreeId?: string;
  ticketId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<WorktreeTicketDetailLoadData> {
  const worktreeId = options.worktreeId ?? '';
  const ticketId = options.ticketId ?? '';
  return {
    worktreeId,
    ticketId,
    result: await ensureTicketDetailLoaded(ticketId, { kind: 'worktree', id: worktreeId }, {
      ...options.loaderOptions,
      depends: options.depends
    })
  };
}
