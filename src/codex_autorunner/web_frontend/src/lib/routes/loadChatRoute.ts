import {
  CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST,
  ensureChatIndexLoaded,
  type ReadModelLoaderResult
} from '$lib/data';
import { readModelLoaderOptions, type LoadReadModelRouteOptions } from './loadReadModelRoute';

export type ChatRouteLoadData = {
  chatId: string | null;
  chatIndex: ReadModelLoaderResult;
  activeDetail: null;
};

const CHAT_INDEX_WINDOW_LIMIT = 50;

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadChatRoute(
  options: LoadReadModelRouteOptions
): Promise<ChatRouteLoadData> {
  const chatId = options.params?.chatId?.trim() || null;
  const chatIndexPromise = ensureChatIndexLoaded(
    { limit: CHAT_INDEX_WINDOW_LIMIT },
    {
      ...readModelLoaderOptions(options),
      refresh: true
    }
  );
  const ticketRunGroupsPromise = ensureChatIndexLoaded(
    { ...CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST, limit: CHAT_INDEX_WINDOW_LIMIT },
    {
      ...readModelLoaderOptions(options),
      refresh: true
    }
  );
  const [chatIndex] = await Promise.all([chatIndexPromise, ticketRunGroupsPromise]);

  return {
    chatId,
    chatIndex,
    activeDetail: null
  };
}
