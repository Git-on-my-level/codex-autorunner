import type { PageLoad } from './$types';
import { loadChatRoute, type ChatRouteLoadData } from './loadChatRoute';

export type { ChatRouteLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<ChatRouteLoadData> => {
  return loadChatRoute({ chatId: params.chatId, depends });
};
