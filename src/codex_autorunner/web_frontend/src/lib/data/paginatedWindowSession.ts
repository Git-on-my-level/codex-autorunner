export type PaginatedWindowRequest = {
  cursor?: string | null;
};

export type PaginatedWindowSnapshot = {
  window?: {
    nextCursor?: string | null;
  } | null;
};

export type PaginatedWindowSession<Request extends PaginatedWindowRequest, Snapshot extends PaginatedWindowSnapshot> = {
  loadMore: (request: Request) => Promise<void>;
};

export function createPaginatedWindowSession<Request extends PaginatedWindowRequest, Snapshot extends PaginatedWindowSnapshot>(deps: {
  key: (request: Request) => string;
  normalize: (request: Request) => Request;
  nextCursor: (request: Request) => string | null | undefined;
  fetchPage: (request: Request) => Promise<Snapshot>;
  appendPage: (snapshot: Snapshot, request: Request) => void;
}): PaginatedWindowSession<Request, Snapshot> {
  const inFlight = new Map<string, Promise<void>>();

  async function loadMore(request: Request): Promise<void> {
    const baseRequest = deps.normalize(request);
    const windowKey = deps.key(baseRequest);
    const nextCursor = deps.nextCursor(baseRequest);
    if (!nextCursor) return;
    const existing = inFlight.get(windowKey);
    if (existing) return existing;
    const promise = deps.fetchPage({ ...baseRequest, cursor: nextCursor })
      .then((snapshot) => {
        deps.appendPage(snapshot, baseRequest);
      })
      .finally(() => {
        inFlight.delete(windowKey);
      });
    inFlight.set(windowKey, promise);
    return promise;
  }

  return { loadMore };
}
