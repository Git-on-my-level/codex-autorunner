import { describe, expect, it } from 'vitest';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from './mockData';
import {
  buildRepoWorktreeDetailViewModel,
  buildRepoWorktreeIndexViewModel,
  countRepoWorktreeIndexEntities,
  filterRepoWorktreeIndexRows,
  visibleRepoWorktreeChildren
} from './repoWorktree';

describe('repo/worktree view models', () => {
  it('builds a lightweight repo index with child worktrees grouped under the repo', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket_id: 'TICKET-110' } }],
      chats: [{ ...mockChatSummary, repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [mockTicketSummary],
      artifacts: []
    });

    expect(vm.rows).toHaveLength(1);
    expect(vm.rows.map((row) => row.id)).toEqual(['repo-1']);
    expect(vm.title).toBe('Repos');
    expect(vm.eyebrow).toBe('Repo ownership');
    expect(vm.activeCount).toBe(2);
    expect(vm.openTicketCount).toBe(4);
    expect(vm.rows[0]).toMatchObject({
      href: '/repos/repo-1',
      pmaChatHref: '/chats?new=repo:repo-1&kind=pma',
      codingAgentChatHref: '/chats?new=repo:repo-1&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 1,
      childWorktrees: [
        {
          href: '/repos/repo-1/worktrees/worktree-1',
          pmaChatHref: '/chats?new=worktree:worktree-1&kind=pma',
          codingAgentChatHref: '/chats?new=worktree:worktree-1&kind=agent',
          currentTicketId: 'TICKET-110',
          currentRunTitle: 'Hub rewrite foundation'
        }
      ]
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
      repoHref: '/repos/repo-1',
      pmaChatHref: '/chats?new=worktree:worktree-1&kind=pma',
      codingAgentChatHref: '/chats?new=worktree:worktree-1&kind=agent',
      signalWaiting: 0,
      signalFailed: 0,
      signalActive: 0
    });
    expect(vm.rows[0].detail).toBeNull();
  });

  it('filters nested worktrees by status the same way search filters nested worktrees', () => {
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

    expect(filterRepoWorktreeIndexRows(vm.rows, '', 'active').map((row) => row.id)).toEqual(['repo-1']);
    expect(visibleRepoWorktreeChildren(vm.rows[0], '', 'active').map((child) => child.id)).toEqual([
      'worktree-active'
    ]);
    expect(countRepoWorktreeIndexEntities(vm.rows)).toBe(3);
    expect(vm.activeCount).toBe(2);
  });

  it('carries PMA signal badges on child worktrees', () => {
    const vm = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [{ ...mockWorktreeSummary, status: 'idle', activeRuns: 0 }],
      runs: [],
      chats: [{ ...mockChatSummary, status: 'waiting', repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [],
      artifacts: []
    });

    expect(vm.rows[0].signalWaiting).toBe(1);
    expect(vm.rows[0].childWorktrees[0]).toMatchObject({ signalWaiting: 1, signalFailed: 0, signalActive: 0 });
  });

  it('builds active current-run detail with links and artifacts', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1' }],
        tickets: [mockTicketSummary],
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
    expect(vm.links.map((link) => link.label)).toContain('View repo tickets');
    expect(vm.links.find((link) => link.label === 'View repo tickets')?.href).toBe('/repos/repo-1/tickets');
    expect(vm.links.map((link) => link.label)).toContain('View repo memory');
    expect(vm.ticketIndexHref).toBe('/repos/repo-1/tickets');
    expect(vm.links.map((link) => link.label)).toContain('Open preview');
    expect(vm.artifacts[0]).toMatchObject({ kind: 'preview_url' });
    expect(vm.childWorktrees).toHaveLength(1);
    expect(vm.childWorktrees[0]).toMatchObject({
      href: '/repos/repo-1/worktrees/worktree-1',
      currentTicketId: 'TICKET-110'
    });
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
    expect(vm.links.find((link) => link.label === 'View worktree tickets')?.href).toBe('/repos/repo-1/worktrees/worktree-1/tickets');
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
        tickets: [{ ...mockTicketSummary, status: 'idle' }],
        artifacts: []
      },
      'repo',
      'repo-1'
    );

    expect(vm.hasActiveRun).toBe(false);
    expect(vm.currentRuns).toHaveLength(0);
    expect(vm.nextTickets[0].title).toBe(mockTicketSummary.title);
    expect(vm.links.find((link) => link.label === 'View repo tickets')).toMatchObject({
      href: '/repos/repo-1/tickets',
      secondary: false
    });
  });

  it('does not mark the fallback current ticket as working when the flow is not active', () => {
    const vm = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [{ ...mockTicketSummary, status: 'invalid', errors: ['frontmatter.agent is required'] }],
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
          { ...mockTicketSummary, id: 'ticket-a', title: 'Repo ticket', repoId: 'repo-1' },
          { ...mockTicketSummary, id: 'ticket-b', title: 'Other repo ticket', repoId: 'repo-2' }
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
          { ...mockTicketSummary, id: 'ticket-scoped', title: 'Repo-owned ticket', repoId: 'repo-1', worktreeId: null },
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
