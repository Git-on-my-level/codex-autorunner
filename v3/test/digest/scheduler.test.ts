import { beforeEach, describe, expect, test } from "bun:test";
import { createDigestScheduler, expireSnoozes } from "../../src/digest/index.ts";
import { stripButtons } from "../../src/surfaces/telegram/render.ts";
import {
  digestRows,
  eventsOfType,
  makeDeps,
  seedHandledDecision,
  seedSession,
  seedSnoozedIncident,
  backdateEvent,
  type TestDeps,
} from "./helpers.ts";

/** digest_time already passed at local noon → the scheduler should fire. */
const DUE = { telegram: { digest_time: "08:30" } };
/** digest_time still ahead at local noon → the scheduler should wait. */
const NOT_YET = { telegram: { digest_time: "13:00" } };

let deps: TestDeps;
let consolidations: number;
let order: string[];

function scheduler(d: TestDeps = deps) {
  return createDigestScheduler(d, async () => {
    consolidations++;
    order.push("consolidation");
  });
}

beforeEach(() => {
  consolidations = 0;
  order = [];
  deps = makeDeps(DUE);
});

describe("daily digest scheduling", () => {
  test("fires once at digest time and never twice in a day", async () => {
    seedHandledDecision(deps.store, { rationale: "approved dep bump" });
    const sched = scheduler();

    await sched.tick();
    expect(deps.channel.digests).toHaveLength(1);
    expect(deps.channel.digests[0]).toContain("approved dep bump");

    const rows = digestRows(deps.store);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.day).toBe("2026-08-26");
    expect(rows[0]!.sent_at).toBe(deps.clock.current.toISOString());

    for (let i = 0; i < 10; i++) {
      deps.clock.advance(60_000);
      await sched.tick();
    }
    expect(deps.channel.digests).toHaveLength(1);
  });

  test("waits until the configured local time", async () => {
    const early = makeDeps(NOT_YET);
    const sched = scheduler(early);
    await sched.tick();
    expect(early.channel.digests).toHaveLength(0);
    expect(digestRows(early.store)).toHaveLength(0);

    // Cross 13:00 local.
    early.clock.advance(70 * 60_000);
    await sched.tick();
    expect(early.channel.digests).toHaveLength(1);
  });

  test("the next day gets its own digest — a day is never skipped", async () => {
    const sched = scheduler();
    await sched.tick();
    deps.clock.advance(24 * 3_600_000);
    await sched.tick();

    expect(deps.channel.digests).toHaveLength(2);
    expect(digestRows(deps.store).map((r) => r.day)).toEqual(["2026-08-26", "2026-08-27"]);
  });

  test("an empty day still produces and persists a digest", async () => {
    await scheduler().tick();
    const [row] = digestRows(deps.store);
    expect(row!.rendered_md).toContain("🌙 Quiet day");
    expect(deps.channel.digests[0]).toContain("🌙 Quiet day");
  });

  test("days the daemon missed get placeholder archive rows", async () => {
    deps.store.kvSet("scheduler.last_digest_day", "2026-08-23");
    await scheduler().tick();

    const days = digestRows(deps.store).map((r) => r.day);
    expect(days).toEqual(["2026-08-23", "2026-08-24", "2026-08-25", "2026-08-26"].slice(1));
    const placeholder = digestRows(deps.store).find((r) => r.day === "2026-08-24")!;
    expect(placeholder.sent_at).toBeNull();
    expect(placeholder.rendered_md).toContain("CAR was not running");
    expect(deps.channel.digests[0]).toContain("No digest was produced for 2026-08-25");
  });

  test("stuck sessions found by the watchdog reach the digest", async () => {
    const session = seedSession(deps.store, { requiresResponse: true, title: "hermes planning" });
    backdateEvent(deps.store, session.eventId, 26);

    await scheduler().tick();
    const md = stripButtons(deps.channel.digests[0]!);
    expect(md).toContain("⚠️ Stuck / silent (1)");
    expect(md).toContain("hermes planning");
    // …and the watchdog's synthetic event exists for triage to pick up.
    expect(eventsOfType(deps.store, "attention.idle")).toHaveLength(1);
  });

  test("messages held for the digest are folded in and not re-held tomorrow", async () => {
    deps.store.enqueueOutbox("telegram", { kind: "notify" }, { text: "quiet-hours notice" });
    deps.store.db.query("UPDATE outbox SET state = 'deferred'").run();

    const sched = scheduler();
    await sched.tick();
    expect(stripButtons(deps.channel.digests[0]!)).toContain("quiet-hours notice");
    expect(
      (deps.store.db.query("SELECT state FROM outbox").get() as { state: string }).state,
    ).toBe("sent");

    deps.clock.advance(24 * 3_600_000);
    await sched.tick();
    expect(deps.channel.digests[1]).not.toContain("quiet-hours notice");
  });

  test("runDigestNow ignores the clock", async () => {
    const early = makeDeps(NOT_YET);
    const data = await scheduler(early).runDigestNow();
    expect(early.channel.digests).toHaveLength(1);
    expect(data.day).toBe("2026-08-26");
  });
});

describe("nightly consolidation", () => {
  test("runs once a day, before the digest is sent", async () => {
    const sched = scheduler();
    await sched.tick();
    expect(consolidations).toBe(1);
    expect(order[0]).toBe("consolidation");
    expect(deps.store.kvGet<string>("scheduler.last_consolidation_day")).toBe("2026-08-26");

    for (let i = 0; i < 5; i++) {
      deps.clock.advance(60_000);
      await sched.tick();
    }
    expect(consolidations).toBe(1);

    deps.clock.advance(24 * 3_600_000);
    await sched.tick();
    expect(consolidations).toBe(2);
  });

  test("has not run before its slot", async () => {
    const early = makeDeps({ telegram: { digest_time: "13:00" } });
    await scheduler(early).tick(); // slot is 12:00; local noon is exactly on it
    expect(consolidations).toBe(1);

    const earlier = makeDeps({ telegram: { digest_time: "20:00" } }); // slot 19:00
    consolidations = 0;
    await scheduler(earlier).tick();
    expect(consolidations).toBe(0);
  });

  test("a throwing consolidation job does not stop the digest", async () => {
    const sched = createDigestScheduler(deps, async () => {
      throw new Error("model unavailable");
    });
    await sched.tick();
    expect(deps.channel.digests).toHaveLength(1);
    const verbs = (deps.store.db.query("SELECT verb FROM audit").all() as { verb: string }[]).map((r) => r.verb);
    expect(verbs).toContain("consolidation.failed");
  });
});

describe("snooze expiry", () => {
  test("an expired snooze reopens the incident and re-notifies", async () => {
    const session = seedSession(deps.store);
    const incident = seedSnoozedIncident(
      deps.store,
      session.carSessionId,
      new Date(deps.clock.current.getTime() - 60_000),
    );

    const reopened = expireSnoozes(deps);
    expect(reopened).toBe(1);

    const row = deps.store.db
      .query("SELECT state, snooze_until FROM incidents WHERE id = ?")
      .get(incident) as { state: string; snooze_until: string | null };
    expect(row.state).toBe("open");
    expect(row.snooze_until).toBeNull();

    const esc = deps.store.db
      .query("SELECT state FROM escalations WHERE incident_id = ?")
      .get(incident) as { state: string };
    expect(esc.state).toBe("pending");

    // Resurfaced as the full card, so David can actually act on it.
    expect(deps.channel.notifies).toHaveLength(0);
    expect(deps.channel.escalations).toHaveLength(1);
    const resurfaced = deps.channel.escalations[0]!;
    expect(resurfaced.incidentId).toBe(incident);
    expect(resurfaced.carSessionId).toBe(session.carSessionId);
    expect(resurfaced.question).toBe("force-push to fix/telemetry-cliff?");
    expect(resurfaced.contextLines).toContain("⏰ Snooze expired — this is back.");
  });

  test("an incident with no escalation falls back to a plain notice", () => {
    const session = seedSession(deps.store);
    deps.store.db
      .query(
        `INSERT INTO incidents (id, car_session_id, opened_by_event, state, snooze_until, summary, opened_at)
         VALUES ('inc_bare', ?, 'evt_seed', 'snoozed', ?, 'multica autopilot silent', ?)`,
      )
      .run(
        session.carSessionId,
        new Date(deps.clock.current.getTime() - 1000).toISOString(),
        deps.clock.current.toISOString(),
      );

    expect(expireSnoozes(deps)).toBe(1);
    expect(deps.channel.escalations).toHaveLength(0);
    expect(deps.channel.notifies[0]!.text).toContain("multica autopilot silent");
    expect(deps.channel.notifies[0]!.carSessionId).toBe(session.carSessionId);
  });

  test("a snooze that has not expired is left alone", () => {
    const session = seedSession(deps.store);
    seedSnoozedIncident(deps.store, session.carSessionId, new Date(deps.clock.current.getTime() + 3_600_000));
    expect(expireSnoozes(deps)).toBe(0);
    expect(deps.channel.notifies).toHaveLength(0);
  });

  test("the scheduler tick expires snoozes", async () => {
    const session = seedSession(deps.store);
    seedSnoozedIncident(deps.store, session.carSessionId, new Date(deps.clock.current.getTime() - 1000));
    await scheduler().tick();
    expect(deps.channel.escalations).toHaveLength(1);
  });

  test("expiry is not repeated on the next tick", async () => {
    const session = seedSession(deps.store);
    seedSnoozedIncident(deps.store, session.carSessionId, new Date(deps.clock.current.getTime() - 1000));
    const sched = scheduler();
    await sched.tick();
    deps.clock.advance(60_000);
    await sched.tick();
    expect(deps.channel.escalations).toHaveLength(1);
  });
});

describe("loop wiring", () => {
  test("start/stop are safe and the loop is named", async () => {
    const sched = scheduler();
    expect(sched.name).toBe("scheduler");
    await sched.start();
    await sched.stop();
    await sched.stop();
  });

  test("the watchdog is throttled between ticks, then runs again", async () => {
    const idle = makeDeps(NOT_YET);
    const sched = scheduler(idle);
    const runs = (): number =>
      (idle.store.db.query("SELECT verb FROM audit").all() as { verb: string }[]).filter(
        (r) => r.verb === "watchdog.ran",
      ).length;

    await sched.tick();
    expect(runs()).toBe(1);
    expect(idle.store.kvGet<string>("scheduler.last_watchdog_at")).toBe(idle.clock.current.toISOString());

    // A minute later is far inside the 15-minute throttle.
    idle.clock.advance(60_000);
    await sched.tick();
    expect(runs()).toBe(1);

    idle.clock.advance(15 * 60_000);
    await sched.tick();
    expect(runs()).toBe(2);
  });
});
