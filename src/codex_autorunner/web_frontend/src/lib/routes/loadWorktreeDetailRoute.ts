import { ensureWorktreeDetailLoaded, type ReadModelLoaderResult } from '$lib/data';
import { loadReadModelRoute, type LoadReadModelRouteOptions } from './loadReadModelRoute';

export type WorktreeDetailLoadData = {
  worktreeId: string;
  result: ReadModelLoaderResult;
};

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadWorktreeDetailRoute(
  options: LoadReadModelRouteOptions & { worktreeId?: string }
): Promise<WorktreeDetailLoadData> {
  const worktreeId = options.worktreeId ?? '';
  return {
    worktreeId,
    result: await loadReadModelRoute({
      ...options,
      load: (loaderOptions) => ensureWorktreeDetailLoaded(worktreeId, loaderOptions)
    })
  };
}
