import {
  ensureWorktreeDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type WorktreeDetailLoadData = {
  worktreeId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeDetailLoadData> => {
  return loadWorktreeDetailRoute({ worktreeId: params.worktreeId, depends });
};

export async function loadWorktreeDetailRoute(options: {
  worktreeId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<WorktreeDetailLoadData> {
  const worktreeId = options.worktreeId ?? '';
  return {
    worktreeId,
    result: await ensureWorktreeDetailLoaded(worktreeId, {
      ...options.loaderOptions,
      depends: options.depends
    })
  };
}
