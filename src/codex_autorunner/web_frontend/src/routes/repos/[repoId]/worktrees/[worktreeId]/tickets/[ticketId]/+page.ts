import type { PageLoad } from './$types';
import { loadWorktreeTicketDetailRoute, type WorktreeTicketDetailLoadData } from '$lib/routes/loadTicketDetailRoute';

export type RepoWorktreeTicketDetailLoadData = WorktreeTicketDetailLoadData;

export const load: PageLoad = async ({ depends, params }): Promise<RepoWorktreeTicketDetailLoadData> => {
  return loadWorktreeTicketDetailRoute({ worktreeId: params.worktreeId, ticketId: params.ticketId, depends });
};
