import type { PageLoad } from './$types';
import { loadRepoDetailRoute, type RepoDetailLoadData } from '$lib/routes/loadRepoDetailRoute';

export type { RepoDetailLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<RepoDetailLoadData> => {
  return loadRepoDetailRoute({ repoId: params.repoId, depends });
};
