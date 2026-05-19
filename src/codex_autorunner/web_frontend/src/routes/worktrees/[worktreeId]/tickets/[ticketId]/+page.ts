import type { PageLoad } from './$types';
import { loadWorktreeTicketDetailRoute, type WorktreeTicketDetailLoadData } from '$lib/routes/loadTicketDetailRoute';

export type { WorktreeTicketDetailLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeTicketDetailLoadData> => {
  return loadWorktreeTicketDetailRoute({ worktreeId: params.worktreeId, ticketId: params.ticketId, depends });
};
