import { describe, expect, it } from 'vitest';
import { isActiveRoute, navGroupLabels, primaryNav } from './navigation';

describe('PMA navigation', () => {
  it('keeps PMA as the primary route without terminal or analytics entries', () => {
    expect(primaryNav[0]?.href).toBe('/pma');
    expect(primaryNav.map((item) => item.href)).not.toContain('/terminal');
    expect(primaryNav.map((item) => item.href)).not.toContain('/analytics');
  });

  it('frames worktrees and durable docs under repo/workspace ownership', () => {
    expect(navGroupLabels.support).toBe('Repos');
    expect(navGroupLabels.workspace).toBe('Workspace indexes');
    expect(primaryNav.map((item) => item.href)).not.toContain('/worktrees');
    expect(primaryNav.find((item) => item.href === '/tickets')).toMatchObject({
      label: 'Workspace tickets',
      group: 'workspace'
    });
    expect(primaryNav.find((item) => item.href === '/contextspace/local')).toMatchObject({
      label: 'Workspace memory',
      group: 'workspace'
    });
  });

  it('matches active top-level routes', () => {
    expect(isActiveRoute('/pma/thread-1', '/pma')).toBe(true);
    expect(isActiveRoute('/tickets/123', '/tickets')).toBe(true);
    expect(isActiveRoute('/contextspace/repo-1', '/contextspace/local')).toBe(true);
    expect(isActiveRoute('/settings', '/settings')).toBe(true);
    expect(isActiveRoute('/repos', '/tickets')).toBe(false);
  });
});
