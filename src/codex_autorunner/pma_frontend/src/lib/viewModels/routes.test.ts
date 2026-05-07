import { describe, expect, it } from 'vitest';
import {
  agentWorkspaceRoute,
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
});
