import {
  ensureWorktreeDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type WorktreeTicketListLoadData = {
  worktreeId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeTicketListLoadData> => {
  return loadWorktreeTicketListRoute({ worktreeId: params.worktreeId, depends });
};

export async function loadWorktreeTicketListRoute(options: {
  worktreeId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<WorktreeTicketListLoadData> {
  const worktreeId = options.worktreeId ?? '';
  return {
    worktreeId,
    result: await ensureWorktreeDetailLoaded(worktreeId, {
      ...options.loaderOptions,
      depends: options.depends,
      blocking: options.loaderOptions?.blocking ?? false
    })
  };
}
