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

    expect(breadcrumbsForPath('/repos/r/worktrees/wt-1')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'r', href: '/repos/r' },
      { label: 'wt-1', href: null }
    ]);
    expect(breadcrumbsForPath('/repos/r/worktrees/wt-1/contextspace')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'r', href: '/repos/r' },
      { label: 'wt-1', href: '/repos/r/worktrees/wt-1' },
      { label: 'Contextspace', href: null }
    ]);
  });

  it('uses primary nav labels for exact top-level routes (no active-route guessing)', () => {
    expect(breadcrumbsForPath('/chats')).toEqual([{ label: 'Chats', href: null }]);
    expect(breadcrumbsForPath('/services')).toEqual([{ label: 'Services', href: null }]);
    expect(breadcrumbsForPath('/settings')).toEqual([{ label: 'Settings', href: null }]);
  });

  it('handles contextspace routes', () => {
    expect(breadcrumbsForPath('/repos/codex-autorunner/contextspace')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'codex-autorunner', href: '/repos/codex-autorunner' },
      { label: 'Contextspace', href: null }
    ]);
  });

  it('covers scoped new-ticket composer routes', () => {
    expect(breadcrumbsForPath('/repos/my-repo/tickets/new')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'my-repo', href: '/repos/my-repo' },
      { label: 'Tickets', href: '/repos/my-repo/tickets' },
      { label: 'New', href: null }
    ]);
    expect(breadcrumbsForPath('/repos/r/worktrees/wt-1/tickets/new')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'r', href: '/repos/r' },
      { label: 'wt-1', href: '/repos/r/worktrees/wt-1' },
      { label: 'Tickets', href: '/repos/r/worktrees/wt-1/tickets' },
      { label: 'New', href: null }
    ]);
  });

  it('covers hub, ticket, and chat scope URLs', () => {
    expect(breadcrumbsForPath('/hub')).toEqual([{ label: 'Hub', href: null }]);
    expect(breadcrumbsForPath('/repos/r/worktrees/wt-1/tickets/42')).toEqual([
      { label: 'Repos', href: '/repos' },
      { label: 'r', href: '/repos/r' },
      { label: 'wt-1', href: '/repos/r/worktrees/wt-1' },
      { label: 'Tickets', href: '/repos/r/worktrees/wt-1/tickets' },
      { label: '#42', href: null }
    ]);
    expect(breadcrumbsForPath('/chats')).toEqual([{ label: 'Chats', href: null }]);
  });

  it('treats / as Chats home', () => {
    expect(breadcrumbsForPath('/')).toEqual([{ label: 'Chats', href: null }]);
  });

  it('falls back for unknown paths with Chats as parent crumb', () => {
    expect(breadcrumbsForPath('/future/feature')).toEqual([
      { label: 'Chats', href: '/chats' },
      { label: 'feature', href: null }
    ]);
  });

  it('builds automations list and detail crumbs', () => {
    expect(breadcrumbsForPath('/automations')).toEqual([{ label: 'Automations', href: null }]);
    expect(breadcrumbsForPath('/automations/daily-security-scan')).toEqual([
      { label: 'Automations', href: '/automations' },
      { label: 'daily-security-scan', href: null }
    ]);
  });

  it('does not expose removed hub routes as named crumbs', () => {
    expect(breadcrumbsForPath('/dashboard')).toEqual([
      { label: 'Chats', href: '/chats' },
      { label: 'dashboard', href: null }
    ]);
    expect(breadcrumbsForPath('/pma-memory')).toEqual([
      { label: 'Chats', href: '/chats' },
      { label: 'pma-memory', href: null }
    ]);
  });
});
