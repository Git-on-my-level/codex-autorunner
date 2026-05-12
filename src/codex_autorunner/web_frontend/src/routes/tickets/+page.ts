import {
  ensureTicketIndexLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type TicketIndexLoadData = {
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends }): Promise<TicketIndexLoadData> => {
  return loadTicketIndexRoute({ depends });
};

export async function loadTicketIndexRoute(options: {
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<TicketIndexLoadData> {
  return {
    result: await ensureTicketIndexLoaded({
      ...options.loaderOptions,
      depends: options.depends
    })
  };
}
