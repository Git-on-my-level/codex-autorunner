import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadRepoIndexPrefs, saveRepoIndexPrefs } from './repoIndexPrefs';

describe('repoIndexPrefs', () => {
  const store: Record<string, string> = {};

  beforeEach(() => {
    for (const key of Object.keys(store)) delete store[key];
    const localStorage = {
      getItem: (key: string) => (key in store ? store[key] : null),
      setItem: (key: string, value: string) => {
        store[key] = value;
      },
      removeItem: (key: string) => {
        delete store[key];
      },
      clear: () => {
        for (const key of Object.keys(store)) delete store[key];
      },
      length: 0,
      key: () => null
    };
    vi.stubGlobal('window', { localStorage });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('persists collapse overrides and hide-stale preference', () => {
    saveRepoIndexPrefs({ collapsed: { repoA: true, repoB: false }, hideStale: true });
    expect(loadRepoIndexPrefs()).toEqual({ collapsed: { repoA: true, repoB: false }, hideStale: true });
  });

  it('sanitizes malformed stored values', () => {
    window.localStorage.setItem(
      'car.web.repos.indexPrefs.v1',
      JSON.stringify({ collapsed: { repoA: true, repoB: 'nope', repoC: false }, hideStale: 'yes' })
    );

    expect(loadRepoIndexPrefs()).toEqual({ collapsed: { repoA: true, repoC: false }, hideStale: false });
  });
});
