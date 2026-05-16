import {
  ensureTicketIndexLoaded,
  ensureTicketDetailLoaded,
  readModelEntityStore,
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
  return loadTicketDetailRoute({
    ticketId: params.ticketId,
    depends,
    loaderOptions: { store: readModelEntityStore }
  });
};

export async function loadTicketDetailRoute(options: {
  ticketId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<TicketDetailLoadData> {
  const ticketId = options.ticketId ?? '';
  const indexResult = await ensureTicketIndexLoaded({
    ...options.loaderOptions,
    depends: options.depends,
    blocking: options.loaderOptions?.blocking ?? false
  });

  const store = options.loaderOptions?.store;
  let detailResult: ReadModelLoaderResult | null = null;
  if (store && ticketId) {
    const state = store.snapshot();
    const summaryIds = state.ticketOrderByOwner['all'];
    const summaries = summaryIds?.map(id => state.ticketSummaries[id]).filter(Boolean) ?? [];
    const matched = summaries.find(
      s =>
        s.id === ticketId &&
        (s.workspaceKind === 'repo' || s.workspaceKind === 'worktree') &&
        s.workspaceId
    );
    if (matched?.workspaceId && (matched.workspaceKind === 'repo' || matched.workspaceKind === 'worktree')) {
      detailResult = await ensureTicketDetailLoaded(ticketId, { kind: matched.workspaceKind, id: matched.workspaceId }, {
        ...options.loaderOptions,
        depends: undefined,
        blocking: options.loaderOptions?.blocking ?? false
      });
    }
  }

  return { ticketId, indexResult, detailResult };
}
