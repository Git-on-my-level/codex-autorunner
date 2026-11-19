/**
 * Read-side helpers over `sessions` / `session_refs`.
 *
 * DESIGN §2: one CAR session may hold multiple native refs (an agentctl exec
 * wrapping a codex process contributes both ids). Adapters need to answer
 * "which native id, of which vendor, in which cwd" before they can reach back.
 */
import type { Store } from "../store/db.ts";

export interface SessionRow {
  car_session_id: string;
  vendor: string;
  host: string;
  title: string | null;
  cwd: string | null;
  repo: string | null;
  state: string;
}

export interface RefRow {
  vendor: string;
  host: string;
  native_id: string;
}

export function getSession(store: Store, carSessionId: string): SessionRow | null {
  return (
    (store.db
      .query(
        "SELECT car_session_id, vendor, host, title, cwd, repo, state FROM sessions WHERE car_session_id = ?",
      )
      .get(carSessionId) as SessionRow | null) ?? null
  );
}

export function getRefs(store: Store, carSessionId: string): RefRow[] {
  return store.db
    .query("SELECT vendor, host, native_id FROM session_refs WHERE car_session_id = ?")
    .all(carSessionId) as RefRow[];
}

/**
 * Pick the native ref to reply through, in caller preference order.
 * Returns null when the session has no ref for any of the wanted vendors.
 */
export function pickRef(store: Store, carSessionId: string, vendors: string[]): RefRow | null {
  const refs = getRefs(store, carSessionId);
  for (const vendor of vendors) {
    const found = refs.find((r) => r.vendor === vendor);
    if (found) return found;
  }
  return null;
}

/** The cwd a resume command should run in, if the session recorded one. */
export function sessionCwd(store: Store, carSessionId: string): string | undefined {
  return getSession(store, carSessionId)?.cwd ?? undefined;
}
