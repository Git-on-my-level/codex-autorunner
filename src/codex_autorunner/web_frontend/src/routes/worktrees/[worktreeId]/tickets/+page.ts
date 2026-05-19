import type { PageLoad } from './$types';
import { loadWorktreeDetailRoute, type WorktreeDetailLoadData } from '$lib/routes/loadWorktreeDetailRoute';

export type WorktreeTicketListLoadData = WorktreeDetailLoadData;

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeTicketListLoadData> => {
  return loadWorktreeDetailRoute({ worktreeId: params.worktreeId, depends });
};
