/**
 * Digest/scheduler test helpers. Local wall-clock times are constructed with
 * `new Date(y, m, d, h, m)` on purpose so the tests assert the same local
 * semantics the scheduler uses, in any TZ.
 */
import { AllowAllPolicy, EmptyMemoryReader, FakeActionBus, FakeChannel, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { DaemonDeps, TriagePort } from "../../src/ports.ts";
import type { Store } from "../../src/store/db.ts";
import { decisionId, escalationId, incidentId } from "../../src/contract/ids.ts";
import { CONTRACT_VERSION, type CarEvent } from "../../src/contract/events.ts";
import { FakeMemoryWriter } from "../telegram/helpers.ts";

export { FakeMemoryWriter };

export const NoopTriage: TriagePort = { tick: async () => 0 };

/** Local noon on 2026-08-26, whatever the machine's TZ is. */
export function localNoonClock(): FakeClock {
  return new FakeClock(new Date(2026, 7, 26, 12, 0, 0, 0));
}

export interface TestDeps extends DaemonDeps {
  store: Store;
  channel: FakeChannel;
  memoryWriter: FakeMemoryWriter;
  clock: FakeClock;
}

export function makeDeps(configOverrides: Record<string, unknown> = {}, clock = localNoonClock()): TestDeps {
  const store = memoryStore(clock);
  const channel = new FakeChannel();
  const memoryWriter = new FakeMemoryWriter();
  return {
    store,
    clock,
    config: testConfig(configOverrides),
    triage: NoopTriage,
    actions: new FakeActionBus(),
    channel,
    memoryReader: new EmptyMemoryReader(),
    memoryWriter,
    policy: new AllowAllPolicy(),
  };
}

export interface SeededSession {
  carSessionId: string;
  eventId: string;
}

export function seedSession(
  store: Store,
  opts: {
    vendor?: string;
    host?: string;
    nativeId?: string;
    title?: string;
    repo?: string;
    requiresResponse?: boolean;
    type?: CarEvent["type"];
  } = {},
): SeededSession {
  const now = store.clock.now().toISOString();
  const event: CarEvent = {
    contract: CONTRACT_VERSION,
    idempotency_key: `seed:${opts.nativeId ?? Math.random()}`,
    ts: now,
    source: { vendor: (opts.vendor ?? "claude-code") as never, host: opts.host ?? "mac-studio", adapter: "hook-http" },
    session: {
      vendor: (opts.vendor ?? "claude-code") as never,
      native_id: opts.nativeId ?? `native-${Math.random().toString(36).slice(2, 8)}`,
      host: opts.host ?? "mac-studio",
      repo_verified: Boolean(opts.repo),
      title: opts.title ?? "multica autopilot #12",
      ...(opts.repo ? { repo: opts.repo } : {}),
    },
    type: opts.type ?? "attention.question",
    severity: "attention",
    requires_response: opts.requiresResponse ?? false,
    response_channel: null,
    title: "needs an answer",
    body: "",
    payload: {},
  };
  const res = store.ingestEvent(event, { verifiedRepo: opts.repo ?? null });
  return { carSessionId: res.car_session_id!, eventId: res.event_id };
}

/** Push an event's received_at into the past without moving the clock. */
export function backdateEvent(store: Store, eventId: string, hoursAgo: number): void {
  const at = new Date(store.clock.now().getTime() - hoursAgo * 3_600_000).toISOString();
  store.db.query("UPDATE events SET received_at = ?, ts = ? WHERE id = ?").run(at, at, eventId);
}

export function setHeartbeat(
  store: Store,
  carSessionId: string,
  opts: { expectedSeconds: number; secondsAgo: number },
): void {
  const at = new Date(store.clock.now().getTime() - opts.secondsAgo * 1000).toISOString();
  store.db
    .query("UPDATE sessions SET expected_heartbeat_s = ?, last_heartbeat_at = ? WHERE car_session_id = ?")
    .run(opts.expectedSeconds, at, carSessionId);
}

export function seedHandledDecision(
  store: Store,
  opts: { carSessionId?: string | null; decidedBy?: string; rationale?: string } = {},
): string {
  const now = store.clock.now().toISOString();
  const inc = incidentId();
  store.db
    .query(
      `INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, opened_at)
       VALUES (?, ?, 'evt_seed', 'resolved', 'dep bump', ?)`,
    )
    .run(inc, opts.carSessionId ?? null, now);
  const dec = decisionId();
  store.db
    .query(
      `INSERT INTO decisions (id, incident_id, decided_by, disposition, action_class, rationale, created_at)
       VALUES (?, ?, ?, 'auto_resolve', 'approve_permission', ?, ?)`,
    )
    .run(dec, inc, opts.decidedBy ?? "rules", opts.rationale ?? "approved dep bump", now);
  return dec;
}

export function seedAnsweredEscalation(
  store: Store,
  opts: { question?: string; approval?: boolean } = {},
): string {
  const now = store.clock.now().toISOString();
  const inc = incidentId();
  store.db
    .query(
      `INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, opened_at, closed_at)
       VALUES (?, NULL, 'evt_seed', 'resolved', '', ?, ?)`,
    )
    .run(inc, now, now);
  const esc = escalationId();
  store.db
    .query(
      `INSERT INTO escalations (id, incident_id, severity, question, state, answered_by, answer_json, answered_at, created_at)
       VALUES (?, ?, 'attention', ?, 'answered', 'david', ?, ?, ?)`,
    )
    .run(
      esc,
      inc,
      opts.question ?? "force-push to fix/telemetry-cliff?",
      JSON.stringify({ approval: opts.approval ?? false }),
      now,
      now,
    );
  return esc;
}

export function seedSnoozedIncident(store: Store, carSessionId: string, snoozeUntil: Date): string {
  const now = store.clock.now().toISOString();
  const inc = incidentId();
  store.db
    .query(
      `INSERT INTO incidents (id, car_session_id, opened_by_event, state, snooze_until, summary, opened_at)
       VALUES (?, ?, 'evt_seed', 'snoozed', ?, 'force-push question', ?)`,
    )
    .run(inc, carSessionId, snoozeUntil.toISOString(), now);
  store.db
    .query(
      `INSERT INTO escalations (id, incident_id, severity, question, state, created_at)
       VALUES (?, ?, 'attention', 'force-push to fix/telemetry-cliff?', 'snoozed', ?)`,
    )
    .run(escalationId(), inc, now);
  return inc;
}

export function digestRows(store: Store): { day: string; rendered_md: string; sent_at: string | null }[] {
  return store.db.query("SELECT * FROM digests ORDER BY day ASC").all() as {
    day: string;
    rendered_md: string;
    sent_at: string | null;
  }[];
}

export function eventsOfType(store: Store, type: string): { id: string; idempotency_key: string; severity: string; title: string }[] {
  return store.db
    .query("SELECT id, idempotency_key, severity, title FROM events WHERE type = ? ORDER BY id ASC")
    .all(type) as { id: string; idempotency_key: string; severity: string; title: string }[];
}
