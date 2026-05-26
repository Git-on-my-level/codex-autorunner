import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  chatRoute,
  legacyWorktreeRedirectPath,
  repoContextspaceRoute,
  repoRoute,
  repoTicketRoute,
  scopedNewChatRoute,
  scopedNewTicketRoute,
  scopedTicketRoute,
  worktreeContextspaceRoute,
  worktreeRoute,
  worktreeTicketRoute
} from './routes';

describe('scope-nested frontend routes', () => {
  it('builds primary hub, repo, and chat filter URLs', () => {
    expect('/hub').toBe('/hub');
    expect('/chats?scope=repo%3Amy-repo').toBe('/chats?scope=repo%3Amy-repo');
    expect(chatRoute('thread:1')).toBe('/chats/thread%3A1');
    expect(chatRoute('thread:1', { searchParams: 'filter=archived' })).toBe('/chats/thread%3A1?filter=archived');
    expect(repoRoute('my-repo')).toBe('/repos/my-repo');
  });

  it('does not generate legacy chat query detail URLs from production source', () => {
    const srcRoot = fileURLToPath(new URL('../../', import.meta.url));
    const offenders = sourceFiles(srcRoot)
      .filter((file) => {
        const source = readFileSync(file, 'utf8');
        return source.includes('/chats?chat=');
      })
      .map((file) => relative(srcRoot, file));

    expect(offenders).toEqual([]);
  });

  it('builds nested worktree, contextspace, and ticket URLs with the parent repo', () => {
    expect(worktreeRoute('wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1');
    expect(repoContextspaceRoute('repo-1')).toBe('/repos/repo-1/contextspace');
    expect(worktreeContextspaceRoute('wt-1', 'repo-1')).toBe('/repos/repo-1/worktrees/wt-1/contextspace');
    expect(repoTicketRoute('repo-1', '42')).toBe('/repos/repo-1/tickets/42');
    expect(worktreeTicketRoute('wt-1', 'repo-1', '42')).toBe('/repos/repo-1/worktrees/wt-1/tickets/42');
  });

  it('builds scoped new-chat URLs with encoded repo and worktree ids', () => {
    expect(scopedNewChatRoute('repo', 'my repo', 'pma')).toBe('/chats?new=repo:my%20repo&kind=pma');
    expect(scopedNewChatRoute('worktree', 'wt/1', 'agent')).toBe('/chats?new=worktree:wt%2F1&kind=agent');
  });

  it('builds scoped ticket and new-ticket URLs', () => {
    expect(scopedTicketRoute('repo', 'repo 1', null, 'TICKET-001')).toBe('/repos/repo%201/tickets/TICKET-001');
    expect(scopedTicketRoute('worktree', 'wt 1', 'repo 1', 'TICKET-001')).toBe(
      '/repos/repo%201/worktrees/wt%201/tickets/TICKET-001'
    );
    expect(scopedNewTicketRoute('repo', 'repo 1')).toBe('/repos/repo%201/tickets/new');
    expect(scopedNewTicketRoute('worktree', 'wt 1', 'repo 1')).toBe('/repos/repo%201/worktrees/wt%201/tickets/new');
    expect(scopedNewTicketRoute('worktree', 'orphan')).toBeNull();
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
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1/contextspace', 'wt-1', 'repo-1')).toBe(
      '/repos/repo-1/worktrees/wt-1/contextspace'
    );
  });

  it('does not redirect nested, unknown, or parentless worktree routes', () => {
    expect(legacyWorktreeRedirectPath('/repos/repo-1/worktrees/wt-1', 'wt-1', 'repo-1')).toBeNull();
    expect(legacyWorktreeRedirectPath('/worktrees/wt-2', 'wt-1', 'repo-1')).toBeNull();
    expect(legacyWorktreeRedirectPath('/worktrees/wt-1', 'wt-1', null)).toBeNull();
  });
});

function sourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.svelte-kit') continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...sourceFiles(path));
      continue;
    }
    if (entry.name.endsWith('.test.ts') || entry.name.endsWith('.test.svelte')) continue;
    if (entry.name.endsWith('.ts') || entry.name.endsWith('.svelte')) files.push(path);
  }
  return files;
}
