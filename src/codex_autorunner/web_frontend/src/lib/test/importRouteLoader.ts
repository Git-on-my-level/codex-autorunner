import { vi } from 'vitest';

const env = vi.hoisted(() => ({ browser: true }));

vi.mock('$app/environment', () => ({
  get browser() {
    return env.browser;
  },
  dev: false,
  building: false,
  version: 'test'
}));

const moduleCache = new Map<string, Promise<Record<string, unknown>>>();

export async function importRouteLoader<T extends Record<string, unknown>>(
  modulePath: string,
  browser: boolean
): Promise<T> {
  env.browser = browser;
  let pending = moduleCache.get(modulePath);
  if (!pending) {
    pending = import(/* @vite-ignore */ modulePath);
    moduleCache.set(modulePath, pending);
  }
  return (await pending) as T;
}
