import { describe, expect, it } from 'vitest';
import { isActiveRoute, primaryNav } from './navigation';

describe('hub navigation', () => {
  it('uses a primary nav focused on chats, repos, automations, and settings', () => {
    expect(primaryNav.map((item) => item.href)).toEqual(['/chats', '/repos', '/automations', '/settings']);
    expect(primaryNav.map((item) => item.href)).not.toContain('/terminal');
    expect(primaryNav.map((item) => item.href)).not.toContain('/analytics');
    expect(primaryNav.map((item) => item.href)).not.toContain('/worktrees');
    expect(primaryNav.map((item) => item.href)).not.toContain('/contextspace/local');
  });

  it('matches active top-level routes', () => {
    expect(isActiveRoute('/', '/chats')).toBe(true);
    expect(isActiveRoute('/chats', '/chats')).toBe(true);
    expect(isActiveRoute('/chats/thread-1', '/chats')).toBe(true);
    expect(isActiveRoute('/repos/abc', '/chats')).toBe(false);
    expect(isActiveRoute('/repos/abc', '/repos')).toBe(true);
    expect(isActiveRoute('/automations', '/automations')).toBe(true);
    expect(isActiveRoute('/settings', '/settings')).toBe(true);
    expect(isActiveRoute('/repos', '/settings')).toBe(false);
  });
});
