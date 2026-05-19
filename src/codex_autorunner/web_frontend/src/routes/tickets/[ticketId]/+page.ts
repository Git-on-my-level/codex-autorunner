import { readModelEntityStore } from '$lib/data';
import type { PageLoad } from './$types';
import { loadTicketDetailRoute, type TicketDetailLoadData } from '$lib/routes/loadTicketDetailRoute';

export type { TicketDetailLoadData };

export const load: PageLoad = async ({ depends, params }): Promise<TicketDetailLoadData> => {
  return loadTicketDetailRoute({
    ticketId: params.ticketId,
    depends,
    loaderOptions: { store: readModelEntityStore }
  });
};
