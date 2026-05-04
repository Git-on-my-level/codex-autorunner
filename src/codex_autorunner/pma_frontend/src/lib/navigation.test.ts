import { describe, expect, it } from 'vitest';
import { isActiveRoute, primaryNav } from './navigation';

describe('PMA navigation', () => {
  it('keeps PMA as the primary route without terminal or analytics entries', () => {
    expect(primaryNav[0]?.href).toBe('/pma');
    expect(primaryNav.map((item) => item.href)).not.toContain('/terminal');
    expect(primaryNav.map((item) => item.href)).not.toContain('/analytics');
  });

  it('matches active top-level routes', () => {
    expect(isActiveRoute('/pma/thread-1', '/pma')).toBe(true);
    expect(isActiveRoute('/tickets/123', '/tickets')).toBe(true);
    expect(isActiveRoute('/contextspace/repo-1', '/contextspace/local')).toBe(true);
    expect(isActiveRoute('/settings', '/settings')).toBe(true);
    expect(isActiveRoute('/repos', '/tickets')).toBe(false);
  });
});
