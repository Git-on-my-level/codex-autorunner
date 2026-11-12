/**
 * In-process registry for parked permission requests (Claude Code hook HTTP
 * responses held open while triage/escalation races the hook deadline).
 *
 * Coordinator-owned shared seam: WS-A (ingest) parks; WS-B tools and WS-E
 * adapters answer. Parks are best-effort by design — they do NOT survive a
 * restart; the durable record is the event row, and an unanswered park simply
 * times out so the vendor falls back to its local prompt (degraded, never broken).
 */

export type PermissionDecision = { decision: "allow" | "deny"; reason?: string };

interface Parked {
  resolve: (d: PermissionDecision | null) => void;
  timer: ReturnType<typeof setTimeout>;
}

const parked = new Map<string, Parked>();

/**
 * Park a permission request keyed by event id. Resolves with a decision, or
 * null on timeout / daemon shutdown (caller then returns "no decision").
 */
export function parkPermission(eventId: string, deadlineMs: number): Promise<PermissionDecision | null> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      parked.delete(eventId);
      resolve(null);
    }, deadlineMs);
    parked.set(eventId, { resolve, timer });
  });
}

/** Answer a parked permission request. Returns false if it already timed out. */
export function answerPermission(eventId: string, decision: PermissionDecision): boolean {
  const entry = parked.get(eventId);
  if (!entry) return false;
  clearTimeout(entry.timer);
  parked.delete(eventId);
  entry.resolve(decision);
  return true;
}

export function hasParkedPermission(eventId: string): boolean {
  return parked.has(eventId);
}

/** Release all parks (shutdown): resolves null so held HTTP responses close. */
export function releaseAllParks(): void {
  for (const [id, entry] of parked) {
    clearTimeout(entry.timer);
    entry.resolve(null);
    parked.delete(id);
  }
}
