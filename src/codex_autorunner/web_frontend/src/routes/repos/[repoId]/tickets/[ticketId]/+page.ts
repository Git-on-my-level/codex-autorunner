import type { PageLoad } from './$types';
import { loadRepoTicketDetailRoute, type RepoTicketDetailLoadData } from '$lib/routes/loadTicketDetailRoute';

export type { RepoTicketDetailLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<RepoTicketDetailLoadData> => {
  return loadRepoTicketDetailRoute({ repoId: params.repoId, ticketId: params.ticketId, depends });
};
