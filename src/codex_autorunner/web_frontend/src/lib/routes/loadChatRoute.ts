import {
  ensureChatDetailLoaded,
  ensureChatIndexLoaded,
  type ReadModelLoaderResult
} from '$lib/data';
import { readModelLoaderOptions, type LoadReadModelRouteOptions } from './loadReadModelRoute';

export type ChatRouteLoadData = {
  chatId: string | null;
  chatIndex: ReadModelLoaderResult;
  activeDetail: ReadModelLoaderResult | null;
};

const CHAT_DETAIL_TIMELINE_LIMIT = 50;
const CHAT_INDEX_WINDOW_LIMIT = 50;

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadChatRoute(
  options: LoadReadModelRouteOptions & { chatId?: string }
): Promise<ChatRouteLoadData> {
  const chatId = options.chatId?.trim() || null;
  const chatIndexPromise = ensureChatIndexLoaded(
    { limit: CHAT_INDEX_WINDOW_LIMIT },
    {
      ...readModelLoaderOptions(options),
      refresh: true
    }
  );
  const ticketRunGroupsPromise = ensureChatIndexLoaded(
    { filter: 'ticket_runs', groupBy: 'ticket_run', limit: CHAT_INDEX_WINDOW_LIMIT },
    {
      ...readModelLoaderOptions(options),
      refresh: true
    }
  );
  if (!chatId) {
    const [chatIndex] = await Promise.all([chatIndexPromise, ticketRunGroupsPromise]);
    return { chatId: null, chatIndex, activeDetail: null };
  }

  const activeDetailPromise = ensureChatDetailLoaded(chatId, {
    ...readModelLoaderOptions(options),
    timelineLimit: CHAT_DETAIL_TIMELINE_LIMIT
  });
  const [chatIndex, activeDetail] = await Promise.all([chatIndexPromise, activeDetailPromise, ticketRunGroupsPromise]);

  return {
    chatId,
    chatIndex,
    activeDetail
  };
}
