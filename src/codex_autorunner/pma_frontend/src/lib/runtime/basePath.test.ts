import { describe, expect, it } from 'vitest';
import { normalizeBasePath, stripRuntimeBasePath, withRuntimeBasePath } from './basePath';

describe('runtime base path helpers', () => {
  it('normalizes injected hub base paths', () => {
    expect(normalizeBasePath('/car/')).toBe('/car');
    expect(normalizeBasePath('car')).toBe('/car');
    expect(normalizeBasePath('/')).toBe('');
    expect(normalizeBasePath(undefined)).toBe('');
  });

  it('prefixes same-origin root paths and leaves external URLs untouched', () => {
    expect(withRuntimeBasePath('/hub/pma/threads', '/car')).toBe('/car/hub/pma/threads');
    expect(withRuntimeBasePath('/chats?chat=thread-1', '/car')).toBe('/car/chats?chat=thread-1');
    expect(withRuntimeBasePath('/car/chats', '/car')).toBe('/car/chats');
    expect(withRuntimeBasePath('https://example.test/chats', '/car')).toBe('https://example.test/chats');
    expect(withRuntimeBasePath('#active-runs', '/car')).toBe('#active-runs');
  });

  it('strips the runtime base path for SvelteKit rerouting', () => {
    expect(stripRuntimeBasePath('/car', '/car')).toBe('/');
    expect(stripRuntimeBasePath('/car/tickets/TICKET-1', '/car')).toBe('/tickets/TICKET-1');
    expect(stripRuntimeBasePath('/chats', '/car')).toBe('/chats');
  });
});
