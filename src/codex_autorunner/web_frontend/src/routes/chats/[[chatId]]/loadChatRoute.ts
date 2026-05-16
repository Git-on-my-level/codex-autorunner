import {
  ensureChatIndexLoaded,
  ensureChatDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';

export type ChatRouteLoadData = {
  chatId: string | null;
  chatIndex: ReadModelLoaderResult;
  activeDetail: ReadModelLoaderResult | null;
};

const CHAT_DETAIL_TIMELINE_LIMIT = 50;
const CHAT_INDEX_WINDOW_LIMIT = 50;

/** Testable helper; must not live in `+page.ts` (SvelteKit allows only reserved route exports there). */
export async function loadChatRoute(options: {
  chatId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<ChatRouteLoadData> {
  const chatId = options.chatId?.trim() || null;
  const chatIndexPromise = ensureChatIndexLoaded(
    { limit: CHAT_INDEX_WINDOW_LIMIT },
    {
      ...options.loaderOptions,
      depends: options.depends,
      refresh: true
    }
  );
  if (!chatId) return { chatId: null, chatIndex: await chatIndexPromise, activeDetail: null };

  const activeDetailPromise = ensureChatDetailLoaded(chatId, {
    ...options.loaderOptions,
    depends: options.depends,
    timelineLimit: CHAT_DETAIL_TIMELINE_LIMIT
  });
  const [chatIndex, activeDetail] = await Promise.all([chatIndexPromise, activeDetailPromise]);

  return {
    chatId,
    chatIndex,
    activeDetail
  };
}
