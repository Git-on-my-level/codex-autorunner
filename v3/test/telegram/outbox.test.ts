import { beforeEach, describe, expect, test } from "bun:test";
import { FakeClock, memoryStore, testConfig, FakeActionBus } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import { createTelegram } from "../../src/surfaces/telegram/index.ts";
import {
  backoffSeconds,
  deliverOutboxOnce,
  enqueueMessage,
  lookupMessage,
} from "../../src/surfaces/telegram/outbox.ts";
import { FakeMemoryWriter, FakeSend, auditVerbs, outboxRows, seedEscalation } from "./helpers.ts";

let clock: FakeClock;
let store: Store;
let send: FakeSend;

beforeEach(() => {
  clock = new FakeClock();
  store = memoryStore(clock);
  send = new FakeSend();
});

describe("backoff", () => {
  test("doubles from 5s and caps", () => {
    expect([1, 2, 3, 4, 5, 6].map((n) => backoffSeconds(n))).toEqual([5, 10, 20, 40, 80, 160]);
    expect(backoffSeconds(20)).toBe(900);
    expect(backoffSeconds(1, 2, 8)).toBe(2);
    expect(backoffSeconds(9, 2, 8)).toBe(8);
  });
});

describe("deliverOutboxOnce", () => {
  test("sends a pending row and records the sent_message_id", async () => {
    enqueueMessage(store, { kind: "notify", chat_id: "-100" }, { text: "hello" });
    const stats = await deliverOutboxOnce(store, send.fn);

    expect(stats).toEqual({ sent: 1, retried: 0, dead: 0, deferred: 0 });
    expect(send.calls).toHaveLength(1);
    expect(send.last!.spec.text).toBe("hello");

    const [row] = outboxRows(store);
    expect(row!.state).toBe("sent");
    expect(row!.attempts).toBe(1);
    expect(row!.sent_message_id).toBe("1001");
    expect(auditVerbs(store)).toContain("outbox.sent");
  });

  test("does not re-send an already sent row", async () => {
    enqueueMessage(store, { kind: "notify" }, { text: "once" });
    await deliverOutboxOnce(store, send.fn);
    await deliverOutboxOnce(store, send.fn);
    expect(send.calls).toHaveLength(1);
  });

  test("respects next_attempt_at", async () => {
    enqueueMessage(store, { kind: "notify" }, { text: "later" });
    store.db.query("UPDATE outbox SET next_attempt_at = ?").run(new Date(clock.current.getTime() + 60_000).toISOString());
    expect((await deliverOutboxOnce(store, send.fn)).sent).toBe(0);
    clock.advance(61_000);
    expect((await deliverOutboxOnce(store, send.fn)).sent).toBe(1);
  });

  test("backs off exponentially on failure and eventually dies", async () => {
    enqueueMessage(store, { kind: "notify" }, { text: "doomed" });
    send.fail = "429 too many requests";

    const expectedDelays = [5, 10, 20, 40, 80];
    for (const delay of expectedDelays) {
      const before = clock.current.getTime();
      const stats = await deliverOutboxOnce(store, send.fn);
      expect(stats.retried).toBe(1);
      const [row] = outboxRows(store);
      expect(row!.state).toBe("pending");
      expect(Date.parse(row!.next_attempt_at) - before).toBe(delay * 1000);
      clock.advance(delay * 1000 + 1);
    }

    // 6th attempt hits maxAttempts.
    const stats = await deliverOutboxOnce(store, send.fn);
    expect(stats).toEqual({ sent: 0, retried: 0, dead: 1, deferred: 0 });
    const [row] = outboxRows(store);
    expect(row!.state).toBe("dead");
    expect(row!.attempts).toBe(6);
    expect(auditVerbs(store)).toContain("outbox.dead");
  });

  test("a recovered send after failures still records the message id", async () => {
    enqueueMessage(store, { kind: "notify" }, { text: "flaky" });
    send.fail = "network";
    await deliverOutboxOnce(store, send.fn);
    send.fail = null;
    clock.advance(6000);
    await deliverOutboxOnce(store, send.fn);

    const [row] = outboxRows(store);
    expect(row!.state).toBe("sent");
    expect(row!.attempts).toBe(2);
    expect(row!.sent_message_id).toBe("1001");
  });

  test("one failing row does not block the rest of the batch", async () => {
    enqueueMessage(store, { kind: "notify" }, { text: "a" });
    enqueueMessage(store, { kind: "notify" }, { text: "b" });
    let first = true;
    const stats = await deliverOutboxOnce(store, async (target, spec) => {
      if (first) {
        first = false;
        throw new Error("boom");
      }
      return send.fn(target, spec);
    });
    expect(stats.retried).toBe(1);
    expect(stats.sent).toBe(1);
  });

  test("unparseable rows are declared dead, not retried forever", async () => {
    store.db
      .query("INSERT INTO outbox (channel, target_json, body_json, next_attempt_at, created_at) VALUES ('telegram','{oops','{}',?,?)")
      .run(clock.current.toISOString(), clock.current.toISOString());
    const stats = await deliverOutboxOnce(store, send.fn);
    expect(stats.dead).toBe(1);
    expect(send.calls).toHaveLength(0);
  });

  test("queue_for_digest parks a row instead of pushing it", async () => {
    enqueueMessage(store, { kind: "notify", queue_for_digest: true }, { text: "quiet hours" });
    const stats = await deliverOutboxOnce(store, send.fn);
    expect(stats.deferred).toBe(1);
    expect(send.calls).toHaveLength(0);
    expect(outboxRows(store)[0]!.state).toBe("deferred");
    expect(auditVerbs(store)).toContain("outbox.deferred_to_digest");
  });
});

describe("delivery side effects", () => {
  test("an escalation send backfills telegram_message_id and the reply-routing map", async () => {
    const seeded = seedEscalation(store);
    enqueueMessage(
      store,
      {
        kind: "escalation",
        escalation_id: seeded.escalationId,
        incident_id: seeded.incidentId,
        car_session_id: seeded.carSessionId,
      },
      { text: "needs you" },
    );
    await deliverOutboxOnce(store, send.fn);

    const esc = store.db
      .query("SELECT telegram_message_id, sent_at FROM escalations WHERE id = ?")
      .get(seeded.escalationId) as { telegram_message_id: string; sent_at: string };
    expect(esc.telegram_message_id).toBe("1001");
    expect(esc.sent_at).toBe(clock.current.toISOString());

    const inc = store.db
      .query("SELECT telegram_message_id FROM incidents WHERE id = ?")
      .get(seeded.incidentId) as { telegram_message_id: string };
    expect(inc.telegram_message_id).toBe("1001");

    expect(lookupMessage(store, "1001")).toEqual({
      escalation_id: seeded.escalationId,
      incident_id: seeded.incidentId,
      car_session_id: seeded.carSessionId,
    });
  });

  test("forum mode creates a topic once and stores telegram_thread_id", async () => {
    const config = testConfig({ telegram: { enabled: true, forum_mode: true, chat_id: "-100" } });
    const channel = createTelegram(store, config, new FakeActionBus(), new FakeMemoryWriter());
    const seeded = seedEscalation(store);

    channel.sendEscalation({
      escalationId: seeded.escalationId,
      incidentId: seeded.incidentId,
      carSessionId: seeded.carSessionId,
      severity: "attention",
      question: "q",
      contextLines: [],
    });
    await channel.deliverOnce(send.fn);

    expect(send.calls[0]!.target.create_topic).toContain("claude-code");
    const session = store.db
      .query("SELECT telegram_thread_id FROM sessions WHERE car_session_id = ?")
      .get(seeded.carSessionId) as { telegram_thread_id: string };
    expect(session.telegram_thread_id).toBe("topic_1001");

    // Second message reuses the topic rather than creating another.
    channel.sendNotify("second", seeded.carSessionId);
    await channel.deliverOnce(send.fn);
    expect(send.calls[1]!.target.create_topic).toBeUndefined();
    expect(send.calls[1]!.target.thread_id).toBe("topic_1001");
  });

  test("flat mode makes the first message the anchor and threads the rest under it", async () => {
    const config = testConfig({ telegram: { enabled: true, forum_mode: false, chat_id: "-100" } });
    const channel = createTelegram(store, config, new FakeActionBus(), new FakeMemoryWriter());
    const seeded = seedEscalation(store);

    channel.sendNotify("first", seeded.carSessionId);
    await channel.deliverOnce(send.fn);
    expect(send.calls[0]!.target.become_anchor).toBe(true);
    expect(store.kvGet<string>(`tg.anchor.${seeded.carSessionId}`)).toBe("1001");

    channel.sendNotify("second", seeded.carSessionId);
    await channel.deliverOnce(send.fn);
    expect(send.calls[1]!.target.reply_to_message_id).toBe("1001");
  });

  test("an edit-in-place row keeps the original message id", async () => {
    enqueueMessage(
      store,
      { kind: "edit", edit_message_id: "777", escalation_id: "esc_x" },
      { text: "resolved" },
    );
    await deliverOutboxOnce(store, send.fn);
    expect(send.last!.target.edit_message_id).toBe("777");
    expect(outboxRows(store)[0]!.sent_message_id).toBe("777");
  });
});
