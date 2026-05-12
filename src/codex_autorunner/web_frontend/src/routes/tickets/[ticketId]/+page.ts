import {
  ensureTicketIndexLoaded,
  ensureTicketDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type TicketDetailLoadData = {
  ticketId: string;
  indexResult: ReadModelLoaderResult;
  detailResult: ReadModelLoaderResult | null;
};

export const load: PageLoad = async ({ depends, params }): Promise<TicketDetailLoadData> => {
  return loadTicketDetailRoute({ ticketId: params.ticketId, depends });
};

export async function loadTicketDetailRoute(options: {
  ticketId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<TicketDetailLoadData> {
  const ticketId = options.ticketId ?? '';
  const indexResult = await ensureTicketIndexLoaded({
    ...options.loaderOptions,
    depends: options.depends
  });

  const store = options.loaderOptions?.store;
  let detailResult: ReadModelLoaderResult | null = null;
  if (store && ticketId) {
    const state = store.snapshot();
    const summaryIds = state.ticketOrderByOwner['all'];
    const summaries = summaryIds?.map(id => state.ticketSummaries[id]).filter(Boolean) ?? [];
    const matched = summaries.find(s => s.workspaceKind === 'repo' && s.workspaceId);
    if (matched?.workspaceId) {
      detailResult = await ensureTicketDetailLoaded(ticketId, { kind: 'repo', id: matched.workspaceId }, {
        ...options.loaderOptions,
        depends: undefined
      });
    }
  }

  return { ticketId, indexResult, detailResult };
}
