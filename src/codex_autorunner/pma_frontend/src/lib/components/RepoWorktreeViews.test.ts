import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import RepoWorktreeViews from './RepoWorktreeViews.svelte';
import { mockArtifact, mockChatSummary, mockRepoSummary, mockRunProgress, mockTicketSummary, mockWorktreeSummary } from '$lib/viewModels/mockData';
import {
  buildRepoWorktreeDetailViewModel,
  buildRepoWorktreeIndexViewModel
} from '$lib/viewModels/repoWorktree';

describe('RepoWorktreeViews', () => {
  it('renders a status-oriented repo/worktree index', () => {
    const index = buildRepoWorktreeIndexViewModel({
      repos: [mockRepoSummary],
      worktrees: [mockWorktreeSummary],
      runs: [mockRunProgress],
      chats: [mockChatSummary],
      tickets: [mockTicketSummary],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'index', index } });

    expect(body).toContain('codex-autorunner');
    expect(body).toContain('discord-5');
    expect(body).toContain('href="/repos/repo-1"');
    expect(body).toContain('href="/repos/repo-1/worktrees/worktree-1"');
    expect(body).toContain('1 worktree');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
  });

  it('renders scoped signal badges on child worktrees', () => {
    const index = buildRepoWorktreeIndexViewModel({
      repos: [{ ...mockRepoSummary, status: 'idle', activeRuns: 0 }],
      worktrees: [{ ...mockWorktreeSummary, status: 'idle', activeRuns: 0 }],
      runs: [],
      chats: [{ ...mockChatSummary, status: 'waiting', repoId: 'repo-1', worktreeId: 'worktree-1' }],
      tickets: [],
      artifacts: []
    });
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'index', index } });

    expect(body).toContain('Scoped PMA chats or runs waiting for attention');
    expect(body).toContain('1 waiting');
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
        artifacts: [mockArtifact]
      },
      'repo',
      'repo-1'
    );
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).toContain('Active run');
    expect(body).toContain('Hub rewrite foundation');
    expect(body).toContain('codex');
    expect(body).toContain('PMA chat');
    expect(body).toContain('View repo tickets');
    expect(body).toContain('href="/repos/repo-1/tickets"');
    expect(body).toContain('View repo memory');
    expect(body).toContain('href="/repos/repo-1/memory"');
    expect(body).toContain('Open preview');
    expect(body).not.toContain('Debug logs');
    expect(body).toContain('Surfaced artifacts');
    expect(body).toContain('Chats');
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
    expect(body).toContain('+80 -5 4 files');
    expect(body).toContain('2m 0s');
  });

  it('renders a no-active-run state without primary terminal or analytics content', () => {
    const detail = buildRepoWorktreeDetailViewModel(
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
    const { body } = render(RepoWorktreeViews, { props: { state: 'ready', mode: 'detail', detail } });

    expect(body).not.toContain('No active run');
    expect(body).not.toContain('Active run');
    expect(body).not.toContain('Create a worktree when a ticket needs isolated repo state.');
    expect(body).toContain('Repo tickets');
    expect(body).toContain('View repo memory');
    expect(body).toContain('href="/repos/repo-1/memory"');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
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
