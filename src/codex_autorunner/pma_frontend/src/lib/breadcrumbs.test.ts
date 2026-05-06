import { describe, expect, it } from 'vitest';
import { breadcrumbsForPath } from './breadcrumbs';

describe('breadcrumbsForPath', () => {
  it('covers repo and worktree chains without duplicating the sidebar group label', () => {
    expect(breadcrumbsForPath('/repos')).toEqual([{ label: 'Repos', href: null }]);
    expect(breadcrumbsForPath('/repos/')).toEqual([{ label: 'Repos', href: null }]);
    expect(breadcrumbsForPath('/repos/my-repo')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'my-repo', href: null }
    ]);
    expect(breadcrumbsForPath('/repos/my%20repo/tickets')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'my repo', href: '/repos/my%20repo' },
      { label: 'Tickets', href: null }
    ]);
    expect(breadcrumbsForPath('/repos/r/tickets/42')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'r', href: '/repos/r' },
      { label: 'Tickets', href: '/repos/r/tickets' },
      { label: '#42', href: null }
    ]);

    expect(breadcrumbsForPath('/worktrees')).toEqual([{ label: 'Worktrees', href: null }]);
    expect(breadcrumbsForPath('/worktrees/wt-1')).toEqual([
      { label: 'Worktrees', href: '/worktrees' },
      { label: 'wt-1', href: null }
    ]);
  });

  it('uses primary nav labels for exact top-level routes (no active-route guessing)', () => {
    expect(breadcrumbsForPath('/pma')).toEqual([{ label: 'PMA', href: null }]);
    expect(breadcrumbsForPath('/dashboard')).toEqual([{ label: 'Dashboard', href: null }]);
    expect(breadcrumbsForPath('/settings')).toEqual([{ label: 'Settings', href: null }]);
    expect(breadcrumbsForPath('/pma-memory')).toEqual([{ label: 'PMA memory', href: null }]);
  });

  it('handles contextspace routes', () => {
    expect(breadcrumbsForPath('/contextspace/codex-autorunner')).toEqual([
      { label: 'Contextspace', href: null },
      { label: 'codex-autorunner', href: null }
    ]);
  });

  it('treats / as PMA home', () => {
    expect(breadcrumbsForPath('/')).toEqual([{ label: 'PMA', href: null }]);
  });

  it('falls back for unknown paths without pretending they are PMA', () => {
    expect(breadcrumbsForPath('/future/feature')).toEqual([
      { label: 'PMA Hub', href: '/pma' },
      { label: 'feature', href: null }
    ]);
  });
});
