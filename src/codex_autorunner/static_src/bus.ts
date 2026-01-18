const listeners = new Map<string, Set<(payload: unknown) => void>>();

export function subscribe(event: string, handler: (payload: unknown) => void): () => void {
  if (!listeners.has(event)) {
    listeners.set(event, new Set());
  }
  const set = listeners.get(event);
  set!.add(handler);
  return () => set!.delete(handler);
}

export function publish(event: string, payload: unknown): void {
  const set = listeners.get(event);
  if (!set) return;
  for (const handler of Array.from(set)) {
    try {
      handler(payload);
    } catch (err) {
      console.error(`Error in '${event}' subscriber`, err);
    }
  }
}
