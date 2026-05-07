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
    title: 'Workspace memory',
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

export function repoMemoryRoute(repoId: string): string {
  return `${repoRoute(repoId)}/memory`;
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

export function worktreeMemoryRoute(worktreeId: string, parentRepoId: string | null = null): string {
  return `${worktreeRoute(worktreeId, parentRepoId)}/memory`;
}

export function agentWorkspaceRoute(workspaceId: string): string {
  return `/agent-workspaces/${encodeURIComponent(workspaceId)}`;
}
