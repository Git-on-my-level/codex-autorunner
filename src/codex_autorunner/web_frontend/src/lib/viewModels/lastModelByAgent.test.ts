import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getLastModelForAgent,
  loadLastModelMap,
  persistLastModelForAgent
} from './lastModelByAgent';

describe('lastModelByAgent', () => {
  const store: Record<string, string> = {};

  beforeEach(() => {
    for (const k of Object.keys(store)) delete store[k];
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

  it('persists and recalls last model per agent id (case-insensitive keys)', () => {
    persistLastModelForAgent('Codex', 'gpt-5');
    expect(getLastModelForAgent('codex')).toBe('gpt-5');
    expect(loadLastModelMap()).toEqual({ codex: 'gpt-5' });
  });

  it('clears storage when model is empty', () => {
    persistLastModelForAgent('opencode', 'm1');
    persistLastModelForAgent('opencode', '');
    expect(getLastModelForAgent('opencode')).toBe('');
    expect(loadLastModelMap()).toEqual({});
  });
});
