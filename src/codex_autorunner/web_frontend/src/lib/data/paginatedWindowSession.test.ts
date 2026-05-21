import { describe, expect, it, vi } from 'vitest';
import { createPaginatedWindowSession } from './paginatedWindowSession';

type Request = {
  filter?: string;
  cursor?: string | null;
};

type Snapshot = {
  rows: string[];
  window: {
    nextCursor: string | null;
  };
};

describe('paginated window session', () => {
  it('loads and appends the next cursor for a canonical window', async () => {
    let nextCursor: string | null = 'page-2';
    const appended: Array<{ snapshot: Snapshot; request: Request }> = [];
    const fetchPage = vi.fn(async (request: Request): Promise<Snapshot> => ({
      rows: [`row:${request.cursor}`],
      window: { nextCursor: null }
    }));
    const session = createPaginatedWindowSession<Request, Snapshot>({
      key: (request) => request.filter ?? 'all',
      normalize: (request) => ({ filter: request.filter ?? 'all' }),
      nextCursor: () => nextCursor,
      fetchPage,
      appendPage: (snapshot, request) => {
        nextCursor = snapshot.window.nextCursor;
        appended.push({ snapshot, request });
      }
    });

    await session.loadMore({ filter: 'archived' });
    await session.loadMore({ filter: 'archived' });

    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(fetchPage).toHaveBeenCalledWith({ filter: 'archived', cursor: 'page-2' });
    expect(appended).toEqual([
      {
        snapshot: { rows: ['row:page-2'], window: { nextCursor: null } },
        request: { filter: 'archived' }
      }
    ]);
  });

  it('deduplicates concurrent loads for the same canonical window', async () => {
    const deferred = createDeferred<Snapshot>();
    const fetchPage = vi.fn(() => deferred.promise);
    const appendPage = vi.fn();
    const session = createPaginatedWindowSession<Request, Snapshot>({
      key: (request) => request.filter ?? 'all',
      normalize: (request) => ({ filter: request.filter ?? 'all' }),
      nextCursor: () => 'page-2',
      fetchPage,
      appendPage
    });

    const first = session.loadMore({ filter: 'archived' });
    const second = session.loadMore({ filter: 'archived' });
    deferred.resolve({ rows: ['row'], window: { nextCursor: null } });
    await Promise.all([first, second]);

    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(appendPage).toHaveBeenCalledTimes(1);
  });
});

function createDeferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}
