import type { TicketSummary } from './domain';

export type TicketCacheOwner = { repo?: string; worktree?: string } | undefined;

type TicketCacheEntry = {
  tickets: TicketSummary[];
  updatedAt: number;
};

const cache = new Map<string, TicketCacheEntry>();

export function rememberTickets(owner: TicketCacheOwner, tickets: TicketSummary[]): void {
  cache.set(cacheKey(owner), { tickets, updatedAt: Date.now() });
}

export function cachedTickets(owner: TicketCacheOwner): TicketSummary[] | null {
  const exact = cache.get(cacheKey(owner));
  if (exact) return exact.tickets;
  if (!owner) return null;
  const allTickets = cache.get(cacheKey(undefined));
  if (!allTickets) return null;
  return allTickets.tickets.filter((ticket) => ownerMatches(ticket, owner));
}

function ownerMatches(ticket: TicketSummary, owner: Exclude<TicketCacheOwner, undefined>): boolean {
  if (owner.repo) return ticket.workspaceKind === 'repo' && ticket.workspaceId === owner.repo;
  if (owner.worktree) return ticket.workspaceKind === 'worktree' && ticket.workspaceId === owner.worktree;
  return false;
}

function cacheKey(owner: TicketCacheOwner): string {
  if (owner?.repo) return `repo:${owner.repo}`;
  if (owner?.worktree) return `worktree:${owner.worktree}`;
  return 'all';
}
