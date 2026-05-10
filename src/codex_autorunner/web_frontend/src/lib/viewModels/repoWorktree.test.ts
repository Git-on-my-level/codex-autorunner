import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from './mockData';
import {
  buildRepoWorktreeDetailViewModel,
  buildRepoWorktreeIndexViewModel,
  countRepoWorktreeIndexEntities,
  filterRepoWorktreeIndexRows,
  visibleRepoWorktreeChildren
} from './repoWorktree';

const repoTicketSummary = {
  ...mockTicketSummary,
  workspaceKind: 'repo' as const,
  workspaceId: 'repo-1',
  workspacePath: '/workspace/codex-autorunner',
  repoId: 'repo-1',
  worktreeId: null
};

describe('repo/worktree view models', () => {
  it('builds a lightweight repo index with child worktrees grouped under the repo', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
      chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [repoTicketSummary],
      artifacts: []
    });

    expect(vm.ticketIndexMetricsAvailable).toBe(true);
    expect(vm.rows).toHaveLength(1);
    expect(vm.rows.map((row) => row.id)).toEqual(['repo-1']);
    expect(vm.title).toBe('Repos');
    expect(vm.eyebrow).toBe('Repo ownership');
    expect(vm.activeCount).toBe(1);
    expect(vm.openTicketCount).toBe(1);
    expect(vm.rows[0]).toMatchObject({
      href: '/repos/repo-1',
      ticketHref: '/repos/repo-1/tickets',
      openTickets: 1,
      totalTickets: 1,
      doneTickets: 0,
      pmaChatHref: '/chats?new=repo:repo-1&kind=pma',
      codingAgentChatHref: '/chats?new=repo:repo-1&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 0,
      childWorktrees: [
        {
          id: 'worktree-1',
          href: '/repos/repo-1/worktrees/worktree-1',
          pmaChatHref: '/chats?new=worktree:worktree-1&kind=pma',
          codingAgentChatHref: '/chats?new=worktree:worktree-1&kind=agent',
          signalActive: 0,
          openTickets: 0,
          totalTickets: 0,
          doneTickets: 0,
          currentTicketId: null,
          currentRunTitle: null
        }
      ]
    });
  });

  it('omits index ticket metrics when the hub ticket list did not load (no snapshot fallback)', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: [],
      ticketsListLoaded: false
    });
    expect(vm.ticketIndexMetricsAvailable).toBe(false);
    expect(vm.openTicketCount).toBe(0);
    expect(vm.rows[0].openTickets).toBe(0);
    expect(vm.rows[0].totalTickets).toBe(0);
    expect(vm.rows[0].doneTickets).toBe(0);
  });

  it('counts repo open tickets from summaries excluding done status', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [],
      runs: [],
      chats: [],
      tickets: [
        {
          ...mockTicketSummary,
          id: 'done-ticket',
          workspaceKind: 'repo',
          workspaceId: 'repo-1',
          repoId: 'repo-1',
          worktreeId: null,
          status: 'done'
        },
        {
          ...mockTicketSummary,
          id: 'open-ticket',
          workspaceKind: 'repo',
          workspaceId: 'repo-1',
          repoId: 'repo-1',
          worktreeId: null,
          status: 'idle'
        }
      ],
      artifacts: []
    });
    expect(vm.ticketIndexMetricsAvailable).toBe(true);
    expect(vm.rows[0].totalTickets).toBe(2);
    expect(vm.rows[0].doneTickets).toBe(1);
    expect(vm.rows[0].openTickets).toBe(1);
  });

  it('counts tickets for child worktree rows on the repo index', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [{ ...mockWorktreeSummary, status: 'idle', activeRuns: 0 }],
      runs: [],
      chats: [],
      tickets: [
        {
          ...mockTicketSummary,
          id: 'done-worktree-ticket',
          workspaceKind: 'worktree',
          workspaceId: 'worktree-1',
          repoId: 'repo-1',
          worktreeId: 'worktree-1',
          status: 'done'
        },
        {
          ...mockTicketSummary,
          id: 'open-worktree-ticket',
          workspaceKind: 'worktree',
          workspaceId: 'worktree-1',
          repoId: 'repo-1',
          worktreeId: 'worktree-1',
          status: 'idle'
        }
      ],
      artifacts: []
    });

    expect(vm.openTicketCount).toBe(1);
    expect(vm.rows[0].openTickets).toBe(0);
    expect(vm.rows[0].childWorktrees[0]).toMatchObject({
      openTickets: 1,
      totalTickets: 2,
      doneTickets: 1
    });
  });

  it('preserves archive eligibility flags on repo child worktree rows', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [
        {
          ...mockWorktreeSummary,
          raw: {
            has_car_state: true,
            unbound_managed_thread_count: 2,
            chat_bound: true,
            cleanup_blocked_by_chat_binding: true
          }
        }
      ],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: []
    });

    expect(vm.rows[0].childWorktrees[0]).toMatchObject({
      hasCarState: true,
      unboundManagedThreadCount: 2,
      chatBound: true,
      cleanupBlockedByChatBinding: true
    });
  });

  it('keeps known child worktrees under their owning repo and only promotes orphan worktrees', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [
        mockWorktreeSummary,
        {
          ...mockWorktreeSummary,
          id: 'orphan-worktree',
          repoId: 'missing-repo',
          name: 'orphan branch',
          branch: 'detached-fixture',
          activeRuns: 0,
          openTickets: 0
        }
      ],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: []
    });

    expect(vm.rows.map((row) => row.id)).toEqual(['repo-1', 'orphan-worktree']);
    expect(vm.rows[0]).toMatchObject({
      id: 'repo-1',
      pmaChatHref: '/chats?new=repo:repo-1&kind=pma',
      codingAgentChatHref: '/chats?new=repo:repo-1&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 0,
      childWorktrees: [
        {
          id: 'worktree-1',
          pmaChatHref: '/chats?new=worktree:worktree-1&kind=pma',
          codingAgentChatHref: '/chats?new=worktree:worktree-1&kind=agent'
        }
      ]
    });
    expect(vm.rows[1]).toMatchObject({
      id: 'orphan-worktree',
      kind: 'worktree',
      repoHref: '/repos/missing-repo',
      ticketHref: '/repos/missing-repo/worktrees/orphan-worktree/tickets',
      pmaChatHref: '/chats?new=worktree:orphan-worktree&kind=pma',
      codingAgentChatHref: '/chats?new=worktree:orphan-worktree&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 0,
      childWorktrees: []
    });
  });

  it('frames the worktree index as repo-owned variants', () => {
    const vm = buildRepoWorktreeIndexViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [],
        chats: [],
        tickets: [],
        artifacts: []
      },
      'worktree'
    );

    expect(vm.title).toBe('Secondary worktree index');
    expect(vm.eyebrow).toBe('Repo-owned variants');
    expect(vm.rows[0]).toMatchObject({
      href: '/repos/repo-1/worktrees/worktree-1',
      ticketHref: '/repos/repo-1/worktrees/worktree-1/tickets',
      repoHref: '/repos/repo-1',
      pmaChatHref: '/chats?new=worktree:worktree-1&kind=pma',
      codingAgentChatHref: '/chats?new=worktree:worktree-1&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 0
    });
    expect(vm.rows[0].detail).toBeNull();
  });

  it('keeps repo-page child worktrees searchable as navigation rows without promoting their status', () => {
    const activeWorktree = {
      ...mockWorktreeSummary,
      id: 'worktree-active',
      name: 'active branch',
      branch: 'active',
      status: 'running' as const,
      activeRuns: 1
    };
    const idleWorktree = {
      ...mockWorktreeSummary,
      id: 'worktree-idle',
      name: 'idle branch',
      branch: 'idle',
      status: 'idle' as const,
      activeRuns: 0
    };
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [activeWorktree, idleWorktree],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: []
    });

    expect(filterRepoWorktreeIndexRows(vm.rows, '', 'active').map((row) => row.id)).toEqual([]);
    expect(visibleRepoWorktreeChildren(vm.rows[0], 'active', 'all').map((child) => child.id)).toEqual([
      'worktree-active'
    ]);
    expect(countRepoWorktreeIndexEntities(vm.rows)).toBe(3);
    expect(vm.activeCount).toBe(0);
  });

  it('does not carry worktree PMA signal badges onto repo-page child navigation rows', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [{ ...mockWorktreeSummary, status: 'idle', activeRuns: 0 }],
      runs: [],
      chats: [{ ...mockChatSummary, status: 'waiting', repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [],
      artifacts: []
    });

    expect(vm.rows[0].signalWaiting).toBe(0);
    expect(vm.rows[0].childWorktrees[0]).toMatchObject({ signalWaiting: 0, signalFailed: 0, signalActive: 0 });
  });

  it('builds active current-run detail with scoped sections and artifacts', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1' }],
        tickets: [mockTicketSummary],
        contextspaceDocs: [
          {
            id: 'spec',
            name: 'spec.md',
            kind: 'spec',
            content: '# Detail spec',
            updatedAt: '2026-05-04T00:01:00Z',
            isPinned: true,
            raw: {}
          }
        ],
        artifacts: [mockArtifact]
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(true);
    expect(vm.currentRuns[0]).toMatchObject({
      title: 'Hub rewrite foundation',
      agentId: 'codex',
      ticketHref: '/repos/repo-1/tickets/TICKET-110',
      chatHref: '/chats?chat=chat-1'
    });
    expect(vm.links.map((link) => link.label)).not.toContain('Open PMA chat');
    expect(vm.ticketIndexHref).toBe('/repos/repo-1/tickets');
    expect(vm.contextspaceHref).toBe('/repos/repo-1/contextspace');
    expect(vm.contextspace.find((doc) => doc.id === 'spec')).toMatchObject({
      filename: 'spec.md',
      summary: 'Detail spec',
      status: 'present',
      href: '/repos/repo-1/contextspace#spec',
      preview: '# Detail spec'
    });
    expect(vm.contextspace.find((doc) => doc.id === 'spec')?.previewHtml).toContain('<h1>Detail spec</h1>');
    expect(vm.contextspace.find((doc) => doc.id === 'active_context')?.preview).toBeNull();
    expect(vm.contextspace[0].id).toBe('spec');
    expect(vm.links.map((link) => link.label)).toContain('Open preview');
    expect(vm.artifacts[0]).toMatchObject({ kind: 'preview_url' });
    expect(vm.childWorktrees).toHaveLength(1);
    expect(vm.childWorktrees[0]).toMatchObject({
      href: '/repos/repo-1/worktrees/worktree-1',
      currentTicketId: null,
      openTickets: 0,
      activeRuns: 0
    });
  });

  it('keeps child worktree ticket-flow runs out of repo detail state', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, id: 'run-wt-only', raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
        chats: [],
        tickets: [mockTicketSummary],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(false);
    expect(vm.flowStatus.status).toBe('idle');
  });

  it('names the base repo on worktree detail when known', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: 'worktree-1' }],
        tickets: [mockTicketSummary],
        artifacts: []
      },
      'worktree',
      'worktree-1'
    );

    expect(vm.baseRepoLabel).toBe('codex-autorunner');
    expect(vm.baseRepoHref).toBe('/repos/repo-1');
    expect(vm.currentRuns[0].ticketHref).toBe('/repos/repo-1/worktrees/worktree-1/tickets/TICKET-110');
    expect(vm.ticketIndexHref).toBe('/repos/repo-1/worktrees/worktree-1/tickets');
    expect(vm.contextspaceHref).toBe('/repos/repo-1/worktrees/worktree-1/contextspace');
  });

  it('does not match repo-level records on worktree detail through the parent repo id', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: null }],
        tickets: [
          {
            ...mockTicketSummary,
            workspaceKind: 'repo',
            workspaceId: 'repo-1',
            workspacePath: mockRepoSummary.path,
            worktreeId: null,
            raw: { repo_id: 'repo-1' }
          }
        ],
        artifacts: []
      },
      'worktree',
      'worktree-1'
    );

    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets).toHaveLength(0);
  });

  it('builds no-active-run detail without promoting debug as primary', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [{ ...repoTicketSummary, status: 'idle' }],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(false);
    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets[0].title).toBe(mockTicketSummary.title);
    expect(vm.ticketIndexHref).toBe('/repos/repo-1/tickets');
    expect(vm.contextspace).toHaveLength(3);
    expect(vm.contextspace.every((doc) => doc.status === 'empty')).toBe(true);
  });

  it('does not mark the fallback current ticket as working when the flow is not active', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [{ ...repoTicketSummary, status: 'invalid', errors: ['frontmatter.agent is required'] }],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.flowStatus.status).toBe('invalid');
    expect(vm.flowStatus.currentTicketId).toBe(mockTicketSummary.id);
    expect(vm.nextTickets[0]).toMatchObject({
      title: mockTicketSummary.title,
      isCurrent: false
    });
  });

  it('scopes queued tickets to the selected repo when ticket ownership is known', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, id: 'repo-1', status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [
          { ...repoTicketSummary, id: 'ticket-a', title: 'Repo ticket', repoId: 'repo-1' },
          { ...repoTicketSummary, id: 'ticket-b', title: 'Other repo ticket', repoId: 'repo-2', workspaceId: 'repo-2' }
        ],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.nextTickets.map((ticket) => ticket.title)).toEqual(['Repo ticket']);
  });

  it('does not use unscoped fallback tickets when scoped tickets exist for a workspace', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, id: 'repo-1', status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [
          { ...repoTicketSummary, id: 'ticket-scoped', title: 'Repo-owned ticket', repoId: 'repo-1', worktreeId: null },
          { ...mockTicketSummary, id: 'ticket-unscoped', title: 'Fallback ticket', repoId: null, worktreeId: null, raw: {} }
        ],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.nextTickets.map((ticket) => ticket.title)).toEqual(['Repo-owned ticket']);
  });

  it('keeps scoped queues empty instead of inheriting unscoped owner-repair tickets', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, id: 'repo-1', status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [
          {
            ...mockTicketSummary,
            id: 'ticket-unscoped',
            title: 'Needs ownership repair',
            workspaceKind: 'unscoped',
            workspaceId: null,
            repoId: null,
            worktreeId: null,
            raw: {}
          }
        ],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.currentTickets).toHaveLength(0);
    expect(vm.nextTickets).toHaveLength(0);
  });

  it('renders unknown repo detail as missing instead of idle workspace state', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'missing-repo', current_ticket_id: 'TICKET-999' } }],
        chats: [{ ...mockChatSummary, repoId: 'missing-repo' }],
        tickets: [{ ...mockTicketSummary, repoId: 'missing-repo', worktreeId: null }],
        artifacts: [mockArtifact]
      },
      'repo',
      'missing-repo'
    );

    expect(vm).toMatchObject({
      isMissing: true,
      title: 'Repo not found',
      stateLabel: 'Missing',
      missingIndexHref: '/repos'
    });
    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets).toHaveLength(0);
    expect(vm.links).toEqual([{ label: 'Back to repos', href: '/repos', secondary: false }]);
  });

  it('renders unknown worktree detail as missing instead of linking to scoped panels', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [],
        chats: [],
        tickets: [],
        artifacts: []
      },
      'worktree',
      'missing-worktree'
    );

    expect(vm).toMatchObject({
      isMissing: true,
      title: 'Worktree not found',
      stateLabel: 'Missing',
      missingIndexHref: '/worktrees',
      ticketIndexHref: '/worktrees'
    });
    expect(vm.baseRepoHref).toBeNull();
  });
});
