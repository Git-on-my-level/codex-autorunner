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

    expect(body).toContain('Current work');
    expect(body).toContain('codex-autorunner');
    expect(body).toContain('discord-5');
    expect(body).toContain('href="/repos/repo-1"');
    expect(body).toContain('href="/worktrees/worktree-1"');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
  });

  it('renders active-run detail with PMA, ticket, contextspace, preview, and secondary logs', () => {
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

    expect(body).toContain('Current run');
    expect(body).toContain('Hub rewrite foundation');
    expect(body).toContain('codex');
    expect(body).toContain('PMA chat');
    expect(body).toContain('View tickets');
    expect(body).toContain('View contextspace');
    expect(body).toContain('href="/contextspace/repo-1"');
    expect(body).toContain('Open preview');
    expect(body).toContain('Debug logs');
    expect(body).toContain('Surfaced artifacts');
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

    expect(body).toContain('No active ticket run is visible');
    expect(body).toContain('Next tickets');
    expect(body).toContain('View contextspace');
    expect(body).toContain('href="/contextspace/repo-1"');
    expect(body).not.toContain('Terminal');
    expect(body).not.toContain('Analytics');
  });
});
