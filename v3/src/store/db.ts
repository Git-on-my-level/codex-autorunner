/**
 * Store root: opens SQLite (WAL), applies migrations, exposes typed repositories.
 *
 * Schema is FROZEN (see migrations.ts). Repository interfaces below are stable;
 * workstreams may run additional read queries against `store.db` from within
 * their own modules, but all writes MUST go through a repository or be added
 * here by the coordinator, and every state-changing write MUST audit.
 */
import { Database } from "bun:sqlite";
import { MIGRATIONS } from "./migrations.ts";
import type { CarEvent, SessionRef } from "../contract/events.ts";
import { sessionKey } from "../contract/events.ts";
import { eventId, sessionId } from "../contract/ids.ts";

export interface Clock {
  now(): Date;
}
export const systemClock: Clock = { now: () => new Date() };

export function openDb(path: string): Database {
  const db = new Database(path, { create: true });
  db.exec("PRAGMA journal_mode = WAL;");
  db.exec("PRAGMA busy_timeout = 5000;");
  db.exec("PRAGMA foreign_keys = ON;");
  const current = (db.query("PRAGMA user_version").get() as { user_version: number }).user_version;
  for (let v = current; v < MIGRATIONS.length; v++) {
    db.transaction(() => {
      db.exec(MIGRATIONS[v]!);
      db.exec(`PRAGMA user_version = ${v + 1}`);
    })();
  }
  return db;
}

export type IngestResult =
  | { inserted: true; event_id: string; car_session_id: string | null }
  | { inserted: false; event_id: string; car_session_id: string | null };

export interface EventRow {
  id: string;
  idempotency_key: string;
  car_session_id: string | null;
  type: string;
  severity: string;
  ts: string;
  received_at: string;
  requires_response: number;
  response_channel_json: string | null;
  title: string;
  body: string;
  payload_json: string;
  expires_at: string | null;
  actor: string;
  triage_state: string;
  incident_id: string | null;
  source_vendor: string;
  source_host: string;
  source_adapter: string;
}

export class Store {
  constructor(
    readonly db: Database,
    readonly clock: Clock = systemClock,
  ) {}

  audit(actor: string, verb: string, objectType: string, objectId: string, detail: unknown = {}): void {
    this.db
      .query(
        "INSERT INTO audit (ts, actor, verb, object_type, object_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
      )
      .run(this.clock.now().toISOString(), actor, verb, objectType, objectId, JSON.stringify(detail));
  }

  /**
   * Idempotent ingest: resolves/creates the session, inserts the event unless the
   * idempotency key already exists. ACK only after this commits.
   */
  ingestEvent(ev: CarEvent, opts: { actor?: "external" | "car" } = {}): IngestResult {
    const now = this.clock.now().toISOString();
    return this.db.transaction((): IngestResult => {
      const carSessionId = ev.session ? this.upsertSession(ev.session, ev.ts) : null;
      const existing = this.db
        .query("SELECT id, car_session_id FROM events WHERE idempotency_key = ?")
        .get(ev.idempotency_key) as { id: string; car_session_id: string | null } | null;
      if (existing) {
        return { inserted: false, event_id: existing.id, car_session_id: existing.car_session_id };
      }
      const id = eventId();
      this.db
        .query(
          `INSERT INTO events (id, idempotency_key, car_session_id, type, severity, ts, received_at,
             requires_response, response_channel_json, title, body, payload_json, expires_at, actor,
             source_vendor, source_host, source_adapter)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .run(
          id,
          ev.idempotency_key,
          carSessionId,
          ev.type,
          ev.severity,
          ev.ts,
          now,
          ev.requires_response ? 1 : 0,
          ev.response_channel ? JSON.stringify(ev.response_channel) : null,
          ev.title,
          ev.body,
          JSON.stringify(ev.payload),
          ev.expires_at ?? null,
          opts.actor ?? "external",
          ev.source.vendor,
          ev.source.host,
          ev.source.adapter,
        );
      if (ev.type === "heartbeat" && carSessionId) {
        this.db
          .query("UPDATE sessions SET last_heartbeat_at = ? WHERE car_session_id = ?")
          .run(ev.ts, carSessionId);
      }
      this.audit("daemon", "event.ingested", "event", id, {
        type: ev.type,
        source: ev.source,
        session: carSessionId,
      });
      return { inserted: true, event_id: id, car_session_id: carSessionId };
    })();
  }

  /** Resolve or create the canonical session for a ref; updates last_event_at. */
  upsertSession(ref: SessionRef, eventTs: string): string {
    const found = this.db
      .query("SELECT car_session_id FROM session_refs WHERE vendor = ? AND host = ? AND native_id = ?")
      .get(ref.vendor, ref.host, ref.native_id) as { car_session_id: string } | null;
    if (found) {
      this.db
        .query(
          `UPDATE sessions SET last_event_at = ?,
             title = COALESCE(?, title), cwd = COALESCE(?, cwd), repo = COALESCE(?, repo)
           WHERE car_session_id = ?`,
        )
        .run(eventTs, ref.title ?? null, ref.cwd ?? null, ref.repo ?? null, found.car_session_id);
      return found.car_session_id;
    }
    const id = sessionId();
    this.db
      .query(
        `INSERT INTO sessions (car_session_id, vendor, host, title, cwd, repo, first_seen, last_event_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(id, ref.vendor, ref.host, ref.title ?? null, ref.cwd ?? null, ref.repo ?? null, eventTs, eventTs);
    this.db
      .query("INSERT INTO session_refs (vendor, host, native_id, car_session_id) VALUES (?, ?, ?, ?)")
      .run(ref.vendor, ref.host, ref.native_id, id);
    this.audit("daemon", "session.created", "session", id, { key: sessionKey(ref) });
    return id;
  }

  /** Link an additional native ref (e.g. codex uuid inside an agentctl exec) to an existing session. */
  linkSessionRef(carSessionId: string, ref: Pick<SessionRef, "vendor" | "host" | "native_id">): void {
    this.db
      .query(
        "INSERT OR IGNORE INTO session_refs (vendor, host, native_id, car_session_id) VALUES (?, ?, ?, ?)",
      )
      .run(ref.vendor, ref.host, ref.native_id, carSessionId);
    this.audit("daemon", "session.ref_linked", "session", carSessionId, ref);
  }

  /**
   * Claim up to `limit` pending events for triage with a lease; expired leases
   * (crashed worker) are reclaimed. Returns claimed rows.
   */
  claimPendingEvents(limit: number, leaseSeconds: number): EventRow[] {
    const now = this.clock.now();
    const leaseUntil = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    return this.db.transaction((): EventRow[] => {
      const rows = this.db
        .query(
          `SELECT * FROM events
           WHERE (triage_state = 'pending')
              OR (triage_state = 'coalescing' AND triage_lease_until < ?)
           ORDER BY received_at ASC LIMIT ?`,
        )
        .all(now.toISOString(), limit) as EventRow[];
      for (const row of rows) {
        this.db
          .query("UPDATE events SET triage_state = 'coalescing', triage_lease_until = ? WHERE id = ?")
          .run(leaseUntil, row.id);
      }
      return rows;
    })();
  }

  setEventTriageState(id: string, state: string, incidentId?: string): void {
    this.db
      .query("UPDATE events SET triage_state = ?, incident_id = COALESCE(?, incident_id) WHERE id = ?")
      .run(state, incidentId ?? null, id);
  }

  enqueueOutbox(channel: string, target: unknown, body: unknown): number {
    const now = this.clock.now().toISOString();
    const res = this.db
      .query(
        "INSERT INTO outbox (channel, target_json, body_json, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?)",
      )
      .run(channel, JSON.stringify(target), JSON.stringify(body), now, now);
    return Number(res.lastInsertRowid);
  }

  recordSpend(provider: string, model: string, tokensIn: number, tokensOut: number, costUsd: number): void {
    const day = this.clock.now().toISOString().slice(0, 10);
    this.db
      .query(
        `INSERT INTO spend (day, provider, model, calls, tokens_in, tokens_out, cost_usd)
         VALUES (?, ?, ?, 1, ?, ?, ?)
         ON CONFLICT(day, provider, model) DO UPDATE SET
           calls = calls + 1, tokens_in = tokens_in + excluded.tokens_in,
           tokens_out = tokens_out + excluded.tokens_out, cost_usd = cost_usd + excluded.cost_usd`,
      )
      .run(day, provider, model, tokensIn, tokensOut, costUsd);
  }

  spendToday(): { cost_usd: number; calls: number } {
    const day = this.clock.now().toISOString().slice(0, 10);
    const row = this.db
      .query("SELECT COALESCE(SUM(cost_usd),0) cost_usd, COALESCE(SUM(calls),0) calls FROM spend WHERE day = ?")
      .get(day) as { cost_usd: number; calls: number };
    return row;
  }

  kvGet<T>(key: string): T | null {
    const row = this.db.query("SELECT value_json FROM kv WHERE key = ?").get(key) as
      | { value_json: string }
      | null;
    return row ? (JSON.parse(row.value_json) as T) : null;
  }

  kvSet(key: string, value: unknown): void {
    this.db
      .query(
        `INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)
         ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at`,
      )
      .run(key, JSON.stringify(value), this.clock.now().toISOString());
  }
}

export function openStore(path: string, clock: Clock = systemClock): Store {
  return new Store(openDb(path), clock);
}
