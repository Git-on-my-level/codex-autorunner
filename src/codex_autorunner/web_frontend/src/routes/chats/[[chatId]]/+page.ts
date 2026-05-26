import type { PageLoad } from './$types';
import { loadChatRoute, type ChatRouteLoadData } from '$lib/routes/loadChatRoute';
import {
  canonicalChatDetailSearchParams,
  legacyChatDetailFromSearchParams
} from '$lib/application/chatDetailSession';
import { chatRoute } from '$lib/viewModels/routes';
import { redirect } from '@sveltejs/kit';

export type { ChatRouteLoadData };

export const load: PageLoad = async ({ depends, params, url }): Promise<ChatRouteLoadData> => {
  const legacyChatId = legacyChatDetailFromSearchParams(url.searchParams);
  if (!params.chatId && legacyChatId) {
    throw redirect(
      307,
      chatRoute(legacyChatId, {
        searchParams: canonicalChatDetailSearchParams(url.searchParams)
      })
    );
  }
  return loadChatRoute({ chatId: params.chatId, searchParams: url.searchParams, depends });
};
