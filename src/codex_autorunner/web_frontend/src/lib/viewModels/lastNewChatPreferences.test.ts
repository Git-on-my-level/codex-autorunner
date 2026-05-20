import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getLastNewChatPreference,
  loadLastNewChatPreferences,
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

  it('persists PMA and coding-agent new-chat scopes separately', () => {
    persistLastNewChatPreference('pma', { scopeId: 'local' });
    persistLastNewChatPreference('agent', { scopeId: 'worktree:wt-1' });

    expect(getLastNewChatPreference('pma')).toEqual({ scopeId: 'local' });
    expect(getLastNewChatPreference('agent')).toEqual({ scopeId: 'worktree:wt-1' });
    expect(loadLastNewChatPreferences()).toEqual({
      pma: { scopeId: 'local' },
      agent: { scopeId: 'worktree:wt-1' }
    });
  });

  it('ignores malformed stored values', () => {
    window.localStorage.setItem('car.web.chat.lastNewChatPreferences.v1', '{"pma":{"scopeId":""},"agent":{"scopeId":42}}');

    expect(loadLastNewChatPreferences()).toEqual({});
  });
});
