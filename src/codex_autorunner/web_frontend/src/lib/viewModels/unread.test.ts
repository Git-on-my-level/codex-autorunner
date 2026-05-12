import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadLastSeenMap, saveLastSeenMap } from './unread';

describe('chat unread marker persistence', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('merges legacy and namespaced local storage keys and writes both back', () => {
    const storage = memoryStorage();
    storage.setItem('pma:lastSeen', JSON.stringify({ 'chat-1': '2026-05-11T10:00:00Z' }));
    storage.setItem(
      'car.web.chat.lastSeen.v1',
      JSON.stringify({
        'chat-1': '2026-05-11T11:00:00Z',
        'chat-2': '2026-05-11T09:00:00Z'
      })
    );
    vi.stubGlobal('window', { localStorage: storage });

    expect(loadLastSeenMap()).toEqual({
      'chat-1': '2026-05-11T11:00:00Z',
      'chat-2': '2026-05-11T09:00:00Z'
    });
    expect(JSON.parse(storage.getItem('pma:lastSeen') ?? '{}')).toEqual(
      JSON.parse(storage.getItem('car.web.chat.lastSeen.v1') ?? '{}')
    );
  });

  it('saves read markers to both stable storage keys', () => {
    const storage = memoryStorage();
    vi.stubGlobal('window', { localStorage: storage });

    saveLastSeenMap({ 'chat-1': '2026-05-11T10:00:00Z' });

    expect(storage.getItem('pma:lastSeen')).toBe('{"chat-1":"2026-05-11T10:00:00Z"}');
    expect(storage.getItem('car.web.chat.lastSeen.v1')).toBe('{"chat-1":"2026-05-11T10:00:00Z"}');
  });
});

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => {
      values.set(key, value);
    }
  };
}
