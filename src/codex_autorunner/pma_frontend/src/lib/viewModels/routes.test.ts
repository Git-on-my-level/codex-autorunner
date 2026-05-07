import { describe, expect, it } from 'vitest';
import {
  agentWorkspaceRoute,
  legacyWorktreeRedirectPath,
  repoMemoryRoute,
  repoRoute,
  repoTicketRoute,
  worktreeMemoryRoute,
  worktreeRoute,
  worktreeTicketRoute
} from './routes';

describe('scope-nested frontend routes', () => {
  it('builds primary hub, repo, chat filter, and agent workspace URLs', () => {
    expect('/hub').toBe('/hub');
    expect('/chats?scope=repo%3Amy-repo').toBe('/chats?scope=repo%3Amy-repo');
    expect(repoRoute('my-repo')).toBe('/repos/my-repo');
    expect(agentWorkspaceRoute('codex-pma')).toBe('/agent-workspaces/codex-pma');
  });

  it('builds nested worktree, memory, and ticket URLs with the parent repo', () => {
    expect(worktreeRoute('wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1');
    expect(repoMemoryRoute('repo-1')).toBe('/repos/repo-1/memory');
    expect(worktreeMemoryRoute('wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1/memory');
    expect(repoTicketRoute('repo-1', '42')).toBe('/repos/repo-1/tickets/42');
    expect(worktreeTicketRoute('wt-1', 'repo-1', '42')).toBe('/repos/repo-1/worktrees/wt-1/tickets/42');
  });

  it('keeps flat worktree URLs only as parentless compatibility fallbacks', () => {
    expect(worktreeRoute('orphan')).toBe('/worktrees/orphan');
    expect(worktreeTicketRoute('orphan')).toBe('/worktrees/orphan/tickets');
  });

  it('redirects removed flat worktree routes to parent repo nested routes when parent is known', () => {
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1', 'wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1');
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1/tickets', 'wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1/tickets');
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1/tickets/TICKET-027', 'wt-1', 'repo-1')).toBe(
      '/repos/repo-1/worktrees/wt-1/tickets/TICKET-027'
    );
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1/memory', 'wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1/memory');
  });

  it('does not redirect nested, unknown, or parentless worktree routes', () => {
    expect(legacyWorktreeRedirectPath('/repos/repo-1/worktrees/wt-1', 'wt-1', 'repo-1')).toBeNull();
    expect(legacyWorktreeRedirectPath('/worktrees/wt-2', 'wt-1', 'repo-1')).toBeNull();
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1', 'wt-1', null)).toBeNull();
  });
});
