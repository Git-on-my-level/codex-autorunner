import { describe, expect, it } from 'vitest';
import { reroute } from './hooks';

describe('SvelteKit reroute hook', () => {
  it('renders projected chat detail URLs through the single /chats route', () => {
    expect(reroute(rerouteEvent('http://localhost/chats/pma%3Aclient-1'))).toBe('/chats');
  });

  it('keeps base-path stripping for non-chat routes', () => {
    globalThis.__CAR_BASE_PATH__ = '/car';
    try {
      expect(reroute(rerouteEvent('http://localhost/car/tickets/TICKET-1'))).toBe('/tickets/TICKET-1');
      expect(reroute(rerouteEvent('http://localhost/car/chats/pma%3Aclient-1'))).toBe('/chats');
    } finally {
      globalThis.__CAR_BASE_PATH__ = undefined;
    }
  });
});

function rerouteEvent(url: string): Parameters<typeof reroute>[0] {
  return { url: new URL(url), fetch };
}
