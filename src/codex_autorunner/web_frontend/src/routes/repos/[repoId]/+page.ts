import {
  ensureRepoDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type RepoDetailLoadData = {
  repoId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<RepoDetailLoadData> => {
  return loadRepoDetailRoute({ repoId: params.repoId, depends });
};

export async function loadRepoDetailRoute(options: {
  repoId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<RepoDetailLoadData> {
  const repoId = options.repoId ?? '';
  return {
    repoId,
    result: await ensureRepoDetailLoaded(repoId, {
      ...options.loaderOptions,
      depends: options.depends,
      blocking: options.loaderOptions?.blocking ?? false
    })
  };
}
