import {
  ensureChatDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type ChatRouteLoadData = {
  chatId: string | null;
  activeDetail: ReadModelLoaderResult | null;
};

export const load: PageLoad = async ({ depends, params }): Promise<ChatRouteLoadData> => {
  return loadChatRoute({ chatId: params.chatId, depends });
};

export async function loadChatRoute(options: {
  chatId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<ChatRouteLoadData> {
  const chatId = options.chatId?.trim() || null;
  if (!chatId) return { chatId: null, activeDetail: null };

  return {
    chatId,
    activeDetail: await ensureChatDetailLoaded(chatId, {
      ...options.loaderOptions,
      depends: options.depends
    })
  };
}
