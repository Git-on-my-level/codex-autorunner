import { beforeEach, describe, expect, test } from "bun:test";
import { runWatchdog } from "../../src/digest/watchdog.ts";
import { localDay } from "../../src/digest/time.ts";
import type { Store } from "../../src/store/db.ts";
import type { CarConfig } from "../../src/config/config.ts";
import type { FakeClock } from "../fakes.ts";
import { backdateEvent, eventsOfType, makeDeps, seedSession, setHeartbeat } from "./helpers.ts";

let store: Store;
let config: CarConfig;
let clock: FakeClock;

beforeEach(() => {
  const deps = makeDeps();
  store = deps.store;
  config = deps.config;
  clock = deps.clock;
});

describe("unanswered requires_response", () => {
  test("synthesizes attention.idle once, and exactly once per day per session", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 6); // config default: 4h

    const first = runWatchdog(store, config, { host: "mac-studio" });
    expect(first.stuck).toHaveLength(1);
    expect(first.stuck[0]!.reason).toBe("pending");
    expect(first.stuck[0]!.label).toContain("1 unanswered ask");
    expect(first.stuck[0]!.label).toContain("6h");
    expect(first.synthesized).toEqual([
      { kind: "idle", carSessionId: session.carSessionId, eventId: expect.any(String), inserted: true },
    ]);

    const idle = eventsOfType(store, "attention.idle");
    expect(idle).toHaveLength(1);
    expect(idle[0]!.idempotency_key).toBe(
      `watchdog:idle:${session.carSessionId}:${localDay(clock.current)}`,
    );
    expect(idle[0]!.severity).toBe("attention");

    // Ticking again the same day must not spam.
    for (let i = 0; i < 5; i++) {
      const again = runWatchdog(store, config, { host: "mac-studio" });
      expect(again.synthesized[0]!.inserted).toBe(false);
    }
    expect(eventsOfType(store, "attention.idle")).toHaveLength(1);
  });

  test("a new day gets a fresh idle event", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 6);
    runWatchdog(store, config);

    clock.advance(24 * 3_600_000);
    backdateEvent(store, session.eventId, 6);
    runWatchdog(store, config);
    expect(eventsOfType(store, "attention.idle")).toHaveLength(2);
  });

  test("the synthetic event attaches to the existing CAR session", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 6);
    runWatchdog(store, config);

    const row = store.db
      .query("SELECT car_session_id, source_vendor, source_adapter, source_host FROM events WHERE type = 'attention.idle'")
      .get() as { car_session_id: string; source_vendor: string; source_adapter: string; source_host: string };
    expect(row.car_session_id).toBe(session.carSessionId);
    expect(row.source_vendor).toBe("other");
    expect(row.source_adapter).toBe("watchdog");
  });

  test("a fresh ask is not stuck yet", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 1);
    expect(runWatchdog(store, config).stuck).toHaveLength(0);
    expect(session.carSessionId).toBeTruthy();
  });

  test("incident closure cannot silence an unresolved request obligation", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 6);
    store.db
      .query(
        "INSERT INTO incidents (id, car_session_id, opened_by_event, state, opened_at) VALUES ('inc_1', ?, ?, 'resolved', ?)",
      )
      .run(session.carSessionId, session.eventId, clock.current.toISOString());
    store.setEventTriageState(session.eventId, "escalated", "inc_1");
    expect(runWatchdog(store, config).stuck).toHaveLength(1);
    store.db.query("UPDATE events SET obligation_state='resolved' WHERE id=?").run(session.eventId);
    expect(runWatchdog(store, config).stuck).toHaveLength(0);
  });

  test("a muted session is left alone", () => {
    const session = seedSession(store, { requiresResponse: true });
    backdateEvent(store, session.eventId, 6);
    store.db
      .query("UPDATE sessions SET muted_until = ? WHERE car_session_id = ?")
      .run(new Date(clock.current.getTime() + 3_600_000).toISOString(), session.carSessionId);
    const res = runWatchdog(store, config);
    expect(res.stuck).toHaveLength(0);
    expect(eventsOfType(store, "attention.idle")).toHaveLength(0);
  });
});

describe("missed heartbeats", () => {
  test("warn multiple produces a digest line only", () => {
    const session = seedSession(store);
    // expected 1h, silent 2.5h → past 2× warn, below 4× escalate.
    setHeartbeat(store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 9000 });

    const res = runWatchdog(store, config);
    expect(res.stuck).toHaveLength(1);
    expect(res.stuck[0]!.reason).toBe("silent");
    expect(res.stuck[0]!.label).toContain("no heartbeat");
    expect(res.stuck[0]!.label).toContain("expected every 1h");
    expect(res.synthesized).toHaveLength(0);
    expect(eventsOfType(store, "attention.error")).toHaveLength(0);
  });

  test("escalate multiple synthesizes attention.error, once per day", () => {
    const session = seedSession(store);
    setHeartbeat(store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 26 * 3600 });

    const res = runWatchdog(store, config);
    expect(res.stuck[0]!.label).toContain("26h");
    expect(res.synthesized[0]).toMatchObject({ kind: "silent", inserted: true });

    const errors = eventsOfType(store, "attention.error");
    expect(errors).toHaveLength(1);
    expect(errors[0]!.idempotency_key).toBe(
      `watchdog:silent:${session.carSessionId}:${localDay(clock.current)}`,
    );
    expect(errors[0]!.title).toContain("Silent 26h");

    runWatchdog(store, config);
    runWatchdog(store, config);
    expect(eventsOfType(store, "attention.error")).toHaveLength(1);
  });

  test("a healthy session is invisible", () => {
    const session = seedSession(store);
    setHeartbeat(store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 600 });
    expect(runWatchdog(store, config).stuck).toHaveLength(0);
  });

  test("sessions with no declared cadence are not judged", () => {
    seedSession(store);
    expect(runWatchdog(store, config).stuck).toHaveLength(0);
  });

  test("ended sessions are exempt", () => {
    const session = seedSession(store);
    setHeartbeat(store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 26 * 3600 });
    store.db.query("UPDATE sessions SET state = 'ended' WHERE car_session_id = ?").run(session.carSessionId);
    expect(runWatchdog(store, config).stuck).toHaveLength(0);
  });

  test("the multiples are configurable", () => {
    const strict = makeDeps({ watchdog: { default_heartbeat_multiple_warn: 10, default_heartbeat_multiple_escalate: 20 } });
    const session = seedSession(strict.store);
    setHeartbeat(strict.store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 5 * 3600 });
    expect(runWatchdog(strict.store, strict.config).stuck).toHaveLength(0);
  });

  test("the watchdog run is audited", () => {
    runWatchdog(store, config);
    const verbs = (store.db.query("SELECT verb FROM audit").all() as { verb: string }[]).map((r) => r.verb);
    expect(verbs).toContain("watchdog.ran");
  });
});
