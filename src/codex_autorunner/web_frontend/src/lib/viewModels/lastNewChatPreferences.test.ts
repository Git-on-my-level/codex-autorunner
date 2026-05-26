import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getLastNewChatPreference,
  loadLastNewChatPreference,
  persistLastNewChatPreference
} from './lastNewChatPreferences';

describe('lastNewChatPreferences', () => {
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

  it('persists one last new-chat scope and kind', () => {
    persistLastNewChatPreference({ scopeId: ' local ', kind: 'pma' });
    expect(getLastNewChatPreference()).toEqual({ scopeId: 'local', kind: 'pma' });

    persistLastNewChatPreference({ scopeId: 'worktree:wt-1', kind: 'agent' });
    expect(getLastNewChatPreference()).toEqual({ scopeId: 'worktree:wt-1', kind: 'agent' });
    expect(loadLastNewChatPreference()).toEqual({ scopeId: 'worktree:wt-1', kind: 'agent' });
  });

  it('ignores malformed stored values', () => {
    window.localStorage.setItem('car.web.chat.lastNewChatPreference.v2', '{"scopeId":"","kind":"agent"}');
    expect(loadLastNewChatPreference()).toBeNull();

    window.localStorage.setItem('car.web.chat.lastNewChatPreference.v2', '{"scopeId":"worktree:wt-1","kind":"other"}');
    expect(loadLastNewChatPreference()).toBeNull();
  });
});
