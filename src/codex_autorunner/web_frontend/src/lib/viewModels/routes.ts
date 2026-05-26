export type FoundationRouteViewModel = {
  eyebrow: string;
  title: string;
  panelTitle: string;
  description: string;
};

export const routeViewModels = {
  repoDetail: {
    eyebrow: 'Repository',
    title: 'Repo detail',
    panelTitle: 'Current execution state',
    description:
      'Repo identity, worktrees, active runs, tickets, and surfaced PMA artifacts will appear here.'
  },
  worktrees: {
    eyebrow: 'Repo children',
    title: 'Repo worktree variants',
    panelTitle: 'Repo worktree index',
    description:
      'Repo-owned worktree status, branch, parent repo, active runs, ticket queue, and last activity will appear here.'
  },
  worktreeDetail: {
    eyebrow: 'Repo worktree',
    title: 'Repo worktree detail',
    panelTitle: 'Execution workspace variant',
    description:
      'Branch state, current PMA work, relevant tickets, previews, and recent artifacts will appear here.'
  },
  ticketDetail: {
    eyebrow: 'Workspace ticket',
    title: 'Workspace ticket detail',
    panelTitle: 'Ticket context',
    description:
      'Ticket state, PMA-created summaries, current run progress, and surfaced artifacts will appear here.'
  },
  contextspace: {
    eyebrow: 'Workspace contextspace',
    title: 'Workspace contextspace',
    panelTitle: 'Durable shared context',
    description:
      'Spec, decisions, active context, and PMA-maintained workspace notes will appear here.'
  }
} satisfies Record<string, FoundationRouteViewModel>;

export function repoRoute(repoId: string): string {
  return `/repos/${encodeURIComponent(repoId)}`;
}

export function repoTicketRoute(repoId: string, ticketId?: string): string {
  const base = `${repoRoute(repoId)}/tickets`;
  return ticketId ? `${base}/${encodeURIComponent(ticketId)}` : base;
}

export function repoContextspaceRoute(repoId: string): string {
  return `${repoRoute(repoId)}/contextspace`;
}

export function worktreeRoute(worktreeId: string, parentRepoId: string | null = null): string {
  const encodedWorktree = encodeURIComponent(worktreeId);
  if (!parentRepoId) return `/worktrees/${encodedWorktree}`;
  return `${repoRoute(parentRepoId)}/worktrees/${encodedWorktree}`;
}

export function worktreeTicketRoute(worktreeId: string, parentRepoId: string | null = null, ticketId?: string): string {
  const base = `${worktreeRoute(worktreeId, parentRepoId)}/tickets`;
  return ticketId ? `${base}/${encodeURIComponent(ticketId)}` : base;
}

export function worktreeContextspaceRoute(worktreeId: string, parentRepoId: string | null = null): string {
  return `${worktreeRoute(worktreeId, parentRepoId)}/contextspace`;
}

export function chatRoute(
  chatId: string,
  options: { searchParams?: URLSearchParams | string } = {}
): string {
  const path = `/chats/${encodeURIComponent(chatId)}`;
  if (!options.searchParams) return path;
  const query = new URLSearchParams(options.searchParams).toString();
  return query ? `${path}?${query}` : path;
}

export type ScopedRouteKind = 'repo' | 'worktree';
export type NewChatRouteKind = 'pma' | 'agent';

export function scopedNewChatRoute(
  kind: ScopedRouteKind,
  id: string,
  chatKind: NewChatRouteKind = 'pma'
): string {
  const scope = kind === 'repo' ? `repo:${encodeURIComponent(id)}` : `worktree:${encodeURIComponent(id)}`;
  return `/chats?new=${scope}&kind=${chatKind}`;
}

export function scopedTicketRoute(
  kind: ScopedRouteKind,
  id: string,
  parentRepoId: string | null = null,
  ticketId?: string
): string {
  return kind === 'repo' ? repoTicketRoute(id, ticketId) : worktreeTicketRoute(id, parentRepoId, ticketId);
}

export function scopedNewTicketRoute(
  kind: ScopedRouteKind,
  id: string,
  parentRepoId: string | null = null
): string | null {
  if (kind === 'worktree' && !parentRepoId) return null;
  return `${scopedTicketRoute(kind, id, parentRepoId)}/new`;
}

export function legacyWorktreeRedirectPath(pathname: string, worktreeId: string, parentRepoId: string | null): string | null {
  if (!parentRepoId) return null;
  const expectedPrefix = `/worktrees/${encodeURIComponent(worktreeId)}`;
  if (pathname !== expectedPrefix && !pathname.startsWith(`${expectedPrefix}/`)) return null;
  const suffix = pathname.slice(expectedPrefix.length);
  if (suffix === '') return worktreeRoute(worktreeId, parentRepoId);
  if (suffix === '/tickets') return worktreeTicketRoute(worktreeId, parentRepoId);
  const ticketMatch = suffix.match(/^\/tickets\/([^/]+)$/);
  if (ticketMatch) return worktreeTicketRoute(worktreeId, parentRepoId, decodeURIComponent(ticketMatch[1]));
  if (suffix === '/contextspace') return worktreeContextspaceRoute(worktreeId, parentRepoId);
  return null;
}
