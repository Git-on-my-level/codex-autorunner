import { ensureRepoDetailLoaded, type ReadModelLoaderResult } from '$lib/data';
import { loadReadModelRoute, type LoadReadModelRouteOptions } from './loadReadModelRoute';

export type RepoDetailLoadData = {
  repoId: string;
  result: ReadModelLoaderResult;
};

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadRepoDetailRoute(
  options: LoadReadModelRouteOptions & { repoId?: string }
): Promise<RepoDetailLoadData> {
  const repoId = options.repoId ?? '';
  return {
    repoId,
    result: await loadReadModelRoute({
      ...options,
      load: (loaderOptions) => ensureRepoDetailLoaded(repoId, loaderOptions)
    })
  };
}
