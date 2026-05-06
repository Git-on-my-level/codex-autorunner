import { describe, expect, it } from 'vitest';
import { isActiveRoute, navGroupLabels, primaryNav } from './navigation';

describe('PMA navigation', () => {
  it('keeps PMA as the primary route without terminal or analytics entries', () => {
    expect(primaryNav[0]?.href).toBe('/pma');
    expect(primaryNav.find((item) => item.href === '/pma-memory')).toMatchObject({
      label: 'PMA memory',
      group: 'primary'
    });
    expect(primaryNav.map((item) => item.href)).not.toContain('/terminal');
    expect(primaryNav.map((item) => item.href)).not.toContain('/analytics');
  });

  it('frames worktrees and durable docs under repo/workspace ownership', () => {
    expect(navGroupLabels.support).toBe('Repos');
    expect(primaryNav.map((item) => item.href)).not.toContain('/worktrees');
    expect(primaryNav.map((item) => item.href)).not.toContain('/tickets');
    expect(primaryNav.map((item) => item.href)).not.toContain('/contextspace/local');
  });

  it('matches active top-level routes', () => {
    expect(isActiveRoute('/pma/thread-1', '/pma')).toBe(true);
    expect(isActiveRoute('/pma-memory', '/pma')).toBe(false);
    expect(isActiveRoute('/pma-memory', '/pma-memory')).toBe(true);
    expect(isActiveRoute('/repos/abc', '/repos')).toBe(true);
    expect(isActiveRoute('/settings', '/settings')).toBe(true);
    expect(isActiveRoute('/repos', '/dashboard')).toBe(false);
  });
});
