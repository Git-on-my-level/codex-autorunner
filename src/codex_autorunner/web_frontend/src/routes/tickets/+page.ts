import type { PageLoad } from './$types';
import { loadTicketIndexRoute, type TicketIndexLoadData } from '$lib/routes/loadTicketDetailRoute';

export type { TicketIndexLoadData };

export const load: PageLoad = async ({ depends }): Promise<TicketIndexLoadData> => {
  return loadTicketIndexRoute({ depends });
};
