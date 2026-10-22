/**
 * Silence watchdog — non-negotiable #3: the absence of expected events is
 * itself an event.
 *
 * Two probes, both idempotent per (session, local day) so a 60s tick can run
 * them forever without spamming: the unique index on `events.idempotency_key`
 * is the dedupe, not a flag in memory.
 */
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import { CONTRACT_VERSION, type CarEvent, type Vendor } from "../contract/events.ts";
import { localDay } from "./time.ts";

export interface StuckLine {
  carSessionId: string | null;
  label: string;
  /** 'pending' = unanswered ask; 'silent' = missed heartbeats. */
  reason: "pending" | "silent";
}

export interface SynthesizedEvent {
  kind: "idle" | "silent";
  carSessionId: string;
  eventId: string;
  inserted: boolean;
}

export interface WatchdogResult {
  stuck: StuckLine[];
  synthesized: SynthesizedEvent[];
}

interface RefRow {
  vendor: string;
  host: string;
  native_id: string;
}

interface SessionSummary {
  car_session_id: string;
  vendor: string;
  host: string;
  title: string | null;
  repo: string | null;
  repo_verified: number;
  cwd: string | null;
  expected_heartbeat_s: number | null;
  last_heartbeat_at: string | null;
  last_event_at: string;
  state: string;
  muted_until: string | null;
}

export function runWatchdog(
  store: Store,
  config: CarConfig,
  opts: { host?: string } = {},
): WatchdogResult {
  const now = store.clock.now();
  const host = opts.host ?? process.env.CAR_HOST ?? "localhost";
  const stuck: StuckLine[] = [];
  const synthesized: SynthesizedEvent[] = [];

  /* --- 1. unanswered requires_response events ------------------------------ */
  const pendingCutoff = new Date(
    now.getTime() - config.watchdog.pending_response_hours * 3_600_000,
  ).toISOString();

  const pendingRows = store.db
    .query(
      `SELECT e.car_session_id AS car_session_id,
              MIN(e.received_at) AS oldest,
              COUNT(*) AS n
       FROM events e
       LEFT JOIN incidents i ON i.id = e.incident_id
       WHERE e.requires_response = 1
         AND e.car_session_id IS NOT NULL
         AND e.received_at < ?
         AND e.obligation_state NOT IN ('resolved','cancelled','expired')
         AND (e.expires_at IS NULL OR e.expires_at > ?)
         AND (i.state IS NULL OR i.state != 'snoozed' OR i.snooze_until IS NULL OR i.snooze_until <= ?)

       GROUP BY e.car_session_id`,
    )
    .all(pendingCutoff, now.toISOString(), now.toISOString()) as { car_session_id: string; oldest: string; n: number }[];

  for (const row of pendingRows) {
    const session = getSessionSummary(store, row.car_session_id);
    if (!session || isMutedNow(session, now)) continue;
    const hours = hoursBetween(row.oldest, now);
    stuck.push({
      carSessionId: row.car_session_id,
      reason: "pending",
      label: `${describe(session)} — ${row.n} unanswered ask${row.n === 1 ? "" : "s"}, oldest ${fmtHours(hours)}`,
    });
    const synth = synthesize(store, session, host, now, {
      kind: "idle",
      type: "attention.idle",
      severity: "attention",
      title: `Waiting on you for ${fmtHours(hours)}`,
      body: `${row.n} event(s) on this session have required a response since ${row.oldest}.`,
      payload: { pending_events: row.n, oldest: row.oldest, watchdog: "pending_response" },
    });
    if (synth) synthesized.push(synth);
  }

  /* --- 2. missed heartbeats ------------------------------------------------ */
  const sessions = store.db
    .query(
      `SELECT * FROM sessions
       WHERE expected_heartbeat_s IS NOT NULL AND expected_heartbeat_s > 0 AND state != 'ended'`,
    )
    .all() as SessionSummary[];

  const warnMult = config.watchdog.default_heartbeat_multiple_warn;
  const escalateMult = config.watchdog.default_heartbeat_multiple_escalate;

  for (const session of sessions) {
    if (isMutedNow(session, now)) continue;
    const last = session.last_heartbeat_at ?? session.last_event_at;
    const ageS = (now.getTime() - Date.parse(last)) / 1000;
    const expected = session.expected_heartbeat_s ?? 0;
    if (ageS < expected * warnMult) continue;

    const overdue = fmtHours(ageS / 3600);
    stuck.push({
      carSessionId: session.car_session_id,
      reason: "silent",
      label: `${describe(session)} — no heartbeat ${overdue} (expected every ${fmtSeconds(expected)})`,
    });

    if (ageS >= expected * escalateMult) {
      const synth = synthesize(store, session, host, now, {
        kind: "silent",
        type: "attention.error",
        severity: "attention",
        title: `Silent ${overdue} — expected heartbeat every ${fmtSeconds(expected)}`,
        body: `Last signal ${last}. Past ${escalateMult}× the expected cadence.`,
        payload: { last_signal: last, expected_heartbeat_s: expected, watchdog: "heartbeat" },
      });
      if (synth) synthesized.push(synth);
    }
  }

  store.audit("daemon", "watchdog.ran", "daemon", "card", {
    stuck: stuck.length,
    synthesized: synthesized.filter((s) => s.inserted).length,
  });
  return { stuck, synthesized };
}

/**
 * Ingest a synthetic event attached to the existing CAR session. The
 * idempotency key is bucketed to the local day, so a session that stays stuck
 * produces exactly one watchdog event per day — never a per-tick flood.
 */
function synthesize(
  store: Store,
  session: SessionSummary,
  host: string,
  now: Date,
  spec: {
    kind: "idle" | "silent";
    type: CarEvent["type"];
    severity: CarEvent["severity"];
    title: string;
    body: string;
    payload: Record<string, unknown>;
  },
): SynthesizedEvent | null {
  const ref = store.db
    .query("SELECT vendor, host, native_id FROM session_refs WHERE car_session_id = ? LIMIT 1")
    .get(session.car_session_id) as RefRow | null;
  if (!ref) return null; // nothing to attach to; the session is unreachable by ref

  const event: CarEvent = {
    contract: CONTRACT_VERSION,
    idempotency_key: `watchdog:${spec.kind}:${session.car_session_id}:${localDay(now)}`,
    ts: now.toISOString(),
    source: { vendor: "other", host, adapter: "watchdog" },
    session: {
      vendor: ref.vendor as Vendor,
      native_id: ref.native_id,
      host: ref.host,
      ...(session.title ? { title: session.title } : {}),
      ...(session.repo ? { repo: session.repo } : {}),
      repo_verified: session.repo_verified === 1,
      ...(session.cwd ? { cwd: session.cwd } : {}),
    },
    type: spec.type,
    severity: spec.severity,
    requires_response: false,
    response_channel: null,
    title: spec.title,
    body: spec.body,
    payload: spec.payload,
  };
  const res = store.ingestEvent(event);
  if (res.inserted) {
    store.audit("daemon", `watchdog.${spec.kind}`, "session", session.car_session_id, {
      event_id: res.event_id,
    });
  }
  return {
    kind: spec.kind,
    carSessionId: session.car_session_id,
    eventId: res.event_id,
    inserted: res.inserted,
  };
}

function getSessionSummary(store: Store, carSessionId: string): SessionSummary | null {
  return (
    (store.db.query("SELECT * FROM sessions WHERE car_session_id = ?").get(carSessionId) as
      | SessionSummary
      | null) ?? null
  );
}

function isMutedNow(session: SessionSummary, now: Date): boolean {
  return Boolean(session.muted_until && session.muted_until > now.toISOString());
}

function describe(session: SessionSummary): string {
  const what = session.title || lastSegment(session.repo) || lastSegment(session.cwd) || session.car_session_id;
  return `${session.vendor} · ${what} @ ${session.host}`;
}

function lastSegment(value: string | null): string | null {
  if (!value) return null;
  const seg = value.split("/").filter(Boolean);
  return seg.length ? seg[seg.length - 1]! : value;
}

function hoursBetween(iso: string, now: Date): number {
  return (now.getTime() - Date.parse(iso)) / 3_600_000;
}

export function fmtHours(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

export function fmtSeconds(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return fmtHours(seconds / 3600);
}
