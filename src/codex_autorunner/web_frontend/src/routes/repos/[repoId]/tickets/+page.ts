import type { PageLoad } from './$types';
import { loadRepoDetailRoute, type RepoDetailLoadData } from '$lib/routes/loadRepoDetailRoute';

export type RepoTicketListLoadData = RepoDetailLoadData;

export const load: PageLoad = async ({ depends, params }): Promise<RepoTicketListLoadData> => {
  return loadRepoDetailRoute({ repoId: params.repoId, depends });
};
