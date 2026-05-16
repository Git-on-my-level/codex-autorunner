import {
  ensureTicketDetailLoaded,
  type ReadModelDepends,
  type ReadModelLoaderOptions,
  type ReadModelLoaderResult
} from '$lib/data';
import type { PageLoad } from './$types';

export type RepoTicketDetailLoadData = {
  repoId: string;
  ticketId: string;
  result: ReadModelLoaderResult;
};

export const load: PageLoad = async ({ depends, params }): Promise<RepoTicketDetailLoadData> => {
  return loadRepoTicketDetailRoute({ repoId: params.repoId, ticketId: params.ticketId, depends });
};

export async function loadRepoTicketDetailRoute(options: {
  repoId?: string;
  ticketId?: string;
  depends?: ReadModelDepends;
  loaderOptions?: ReadModelLoaderOptions;
}): Promise<RepoTicketDetailLoadData> {
  const repoId = options.repoId ?? '';
  const ticketId = options.ticketId ?? '';
  return {
    repoId,
    ticketId,
    result: await ensureTicketDetailLoaded(ticketId, { kind: 'repo', id: repoId }, {
      ...options.loaderOptions,
      depends: options.depends,
      blocking: options.loaderOptions?.blocking ?? false
    })
  };
}
