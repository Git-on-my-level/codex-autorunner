import type { PageLoad } from './$types';
import { loadWorktreeDetailRoute, type WorktreeDetailLoadData } from '$lib/routes/loadWorktreeDetailRoute';

export type { WorktreeDetailLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<WorktreeDetailLoadData> => {
  return loadWorktreeDetailRoute({ worktreeId: params.worktreeId, depends });
};
