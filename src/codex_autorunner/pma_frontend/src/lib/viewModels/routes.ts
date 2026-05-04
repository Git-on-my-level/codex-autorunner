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
