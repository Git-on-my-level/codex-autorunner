import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import RepoWorktreeViews from './RepoWorktreeViews.svelte';
import { mockArtifact, mockChatSummary, mockContextspaceDocument, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from '$lib/viewModels/mockData';
import {
  buildRepoWorktreeDetailViewModel,
  buildRepoWorktreeIndexViewModel
} from '$lib/viewModels/repoWorktree';

describe('RepoWorktreeViews', () => {
  it('renders a status-oriented repo/worktree index', () => {
    const repoScopedTicket = {
      ...mockTicketSummary,
      id: 'TICKET-111',
      number: 111,
      workspaceKind: 'repo' as const,
      workspaceId: 'repo-1',
      repoId: 'repo-1',
      worktreeId: null,
      status: 'idle' as const
    };
    const index = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      tickets: [mockTicketSummary, repoScopedTicket],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'index', index } });

    expect(body).toContain('codex-autorunner');
    expect(body).toContain('discord-5');
    expect(body).toContain('href="/repos/repo-1"');
    expect(body).toContain('href="/repos/repo-1/tickets"');
    expect(body).toContain('href="/repos/repo-1/worktrees/worktree-1"');
    expect(body).toContain('1 worktree');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
  });

  it('keeps child worktree rows as navigation without scoped signal badges on the repo page', () => {
    const index = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [{ ...mockWorktreeSummary, status: 'idle', activeRuns: 0 }],
      runs: [],
      chats: [{ ...mockChatSummary, status: 'waiting', repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'index', index } });

    expect(body).toContain('discord-5');
    expect(body).not.toContain('Scoped PMA chats or runs waiting for attention');
    expect(body).not.toContain('1 waiting');
  });

  it('renders repo archive plus child worktree archive and cleanup actions', () => {
    const index = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, raw: { has_car_state: true } }],
      worktrees: [{ ...mockWorktreeSummary, raw: { has_car_state: true, chat_bound: true, cleanup_blocked_by_chat_binding: true } }],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, {
      props: {
        state: 'ready',
        mode: 'index',
        index,
        onCleanupWorktree: () => undefined,
        onArchiveState: () => undefined
      }
    });

    expect(body).toContain('Archive CAR state for codex-autorunner');
    expect(body).toContain('Archive CAR state for discord-5');
    expect(body).toContain('Cleanup worktree discord-5');
    expect(body).toContain('icon-action cleanup');
    expect(body).toContain('icon-action archive');
  });

  it('renders sparse repo index empty-state copy', () => {
    const index = buildRepoWorktreeIndexViewModel({
      repos: [],
      worktrees: [],
      runs: [],
      chats: [],
      tickets: [],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'index', index } });

    expect(body).toContain('No repos registered');
    expect(body).toContain('Register a workspace before queueing repo-scoped tickets.');
  });

  it('renders active-run detail with chat, ticket, contextspace, and preview', () => {
    const detail = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { repo_id: 'repo-1', current_ticket_id: 'TICKET-110' } }],
        chats: [{ ...mockChatSummary, repoId: 'repo-1' }],
        tickets: [mockTicketSummary],
        contextspaceDocs: [mockContextspaceDocument],
        artifacts: [mockArtifact]
      },
      'repo',
      'repo-1'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).toContain('Active run');
    expect(body).toContain('Hub rewrite foundation');
    expect(body).toContain('codex');
    expect(body).toContain('Chat');
    expect(body).toContain('href="/repos/repo-1/tickets"');
    expect(body).toContain('Contextspace');
    expect(body).toContain('spec.md');
    expect(body).toContain('Spec');
    expect(body).toContain('href="/repos/repo-1/contextspace#active_context"');
    expect(body).not.toContain('Debug logs');
    expect(body).toContain('Surfaced artifacts');
    expect(body).toContain('Chats');
    expect(body).toContain('configured model');
    expect(body).not.toContain('Child worktrees');
    expect(body).not.toContain('Activity');
    expect(body).not.toContain('Open PMA chat');
  });

  it('renders worktree detail ticket-flow strip and current queue row affordances', () => {
    const detail = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [{ ...mockRunProgress, raw: { worktree_id: 'worktree-1', current_ticket: 'TICKET-110.md', turn_count: 3 } }],
        chats: [{ ...mockChatSummary, worktreeId: 'worktree-1' }],
        tickets: [{ ...mockTicketSummary, raw: { body: 'Scoped worktree body preview.' } }],
        contextspaceDocs: [mockContextspaceDocument],
        artifacts: [mockArtifact]
      },
      'worktree',
      'worktree-1'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).toContain('Ticket flow status');
    expect(body).toContain('Done/total');
    expect(body).toContain('#110 Implement typed UI API client and view models');
    expect(body).toContain('workspace-ticket-row running');
    expect(body).toContain('working-badge');
    expect(body).toContain('Scoped worktree body preview.');
    expect(body).toContain('+80');
    expect(body).toContain('-5');
    expect(body).toMatch(/4\s+files/);
    expect(body).toContain('2m 0s');
    expect(body).not.toContain('contextspace-row-kind');
  });

  it('renders a no-active-run state without primary terminal or analytics content', () => {
    const detail = buildRepoWorktreeDetailViewModel(
      {
        repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [],
        contextspaceDocs: [],
        artifacts: []
      },
      'repo',
      'repo-1'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).not.toContain('No active run');
    expect(body).not.toContain('Active run');
    expect(body).not.toContain('Create a worktree when a ticket needs isolated repo state.');
    expect(body).toContain('Repo tickets');
    expect(body).toContain('No tickets');
    expect(body).toContain('No scoped tickets are queued for this repo.');
    expect(body).toContain('Contextspace');
    expect(body).toContain('active_context.md');
    expect(body).toContain('No context recorded');
    expect(body).toContain('href="/repos/repo-1/contextspace#active_context"');
    expect(body).toContain('Chats');
    expect(body).toContain('href="/chats?new=repo:repo-1&amp;kind=pma"');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
    const tickets = body.indexOf('Repo tickets');
    const contextspace = body.indexOf('Contextspace');
    const chats = body.indexOf('>Chats<');
    expect(contextspace).toBeGreaterThan(-1);
    expect(contextspace).toBeLessThan(tickets);
    expect(tickets).toBeLessThan(chats);
  });

  it('renders git status pills and an inline spec preview when data is available', () => {
    const detail = buildRepoWorktreeDetailViewModel(
      {
        repos: [
          {
            ...mockRepoSummary,
            gitStatus: {
              branch: 'main',
              dirty: true,
              filesChanged: 3,
              insertions: 42,
              deletions: 7,
              untracked: 1,
              staged: null,
              hasUpstream: true,
              ahead: 2,
              behind: 1
            }
          }
        ],
        worktrees: [],
        runs: [],
        chats: [],
        tickets: [],
        contextspaceDocs: [
          {
            id: 'spec',
            name: 'spec.md',
            kind: 'spec',
            content: '# Build the thing\n\n## Goal\n- Ship feature X\n- Make it fast',
            updatedAt: '2026-05-04T00:01:00Z',
            isPinned: true,
            raw: {}
          }
        ],
        artifacts: []
      },
      'repo',
      'repo-1'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).toContain('Dirty');
    expect(body).toContain('3 files changed');
    expect(body).toContain('+42');
    expect(body).toContain('-7');
    expect(body).toContain('1 untracked');
    expect(body).toContain('↑ 2 ahead');
    expect(body).toContain('↓ 1 behind');
    expect(body).toContain('contextspace-spec-preview');
    expect(body).toContain('<h1>Build the thing</h1>');
    expect(body).toContain('Ship feature X');
  });

  it('renders unknown detail as an explicit missing-resource state', () => {
    const detail = buildRepoWorktreeDetailViewModel(
      {
        repos: [mockRepoSummary],
        worktrees: [mockWorktreeSummary],
        runs: [],
        chats: [],
        tickets: [],
        artifacts: []
      },
      'repo',
      'missing-repo'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).toContain('Repo not found');
    expect(body).toContain('does not match a known repo');
    expect(body).toContain('href="/repos"');
    expect(body).toContain('Back to repos');
    expect(body).not.toContain('No active run');
    expect(body).not.toContain('href="/contextspace/missing-repo"');
  });
});
