import type { PageLoad } from './$types';
import { loadChatRoute, type ChatRouteLoadData } from '$lib/routes/loadChatRoute';

export type { ChatRouteLoadData };

export const load: PageLoad = async ({ depends }): Promise<ChatRouteLoadData> => {
  return loadChatRoute({ depends });
};
