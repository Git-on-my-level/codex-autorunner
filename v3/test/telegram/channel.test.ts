import { beforeEach, describe, expect, test } from "bun:test";
import { FakeActionBus, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import { createTelegram, type TelegramChannel } from "../../src/surfaces/telegram/index.ts";
import { FakeMemoryWriter, FakeSend, auditVerbs, outboxRows, seedEscalation, testSafety } from "./helpers.ts";

let clock: FakeClock;
let store: Store;
let channel: TelegramChannel;

function build(configOverrides: Record<string, unknown> = {}): TelegramChannel {
  return createTelegram(
    store,
    testConfig({ telegram: { chat_id: "-100", ...configOverrides } }),
    new FakeActionBus(),
    new FakeMemoryWriter(),
    testSafety(store),
  );
}

beforeEach(() => {
  clock = new FakeClock();
  store = memoryStore(clock);
  channel = build();
});

describe("ChannelPort enqueues rich specs", () => {
  test("sendEscalation renders the card and targets the chat", () => {
    const seeded = seedEscalation(store, { title: "fix BLE reconnect", host: "mac-studio" });
    channel.sendEscalation({
      escalationId: seeded.escalationId,
      incidentId: seeded.incidentId,
      carSessionId: seeded.carSessionId,
      severity: "attention",
      question: "force-push to fix/telemetry-cliff?",
      contextLines: ["CAR probed: remote has 2 CI commits."],
      suggestedActionLabel: "DENY",
    });

    const [row] = outboxRows(store);
    expect(row!.target).toMatchObject({
      kind: "escalation",
      chat_id: "-100",
      escalation_id: seeded.escalationId,
      incident_id: seeded.incidentId,
      car_session_id: seeded.carSessionId,
    });
    expect(row!.body.text).toContain("🔴 needs you · claude-code · fix BLE reconnect @ mac-studio");
    expect(row!.body.text).toContain("Suggests: DENY");
    expect(row!.body.inline_keyboard!.flat().map((b) => b.text)).toEqual([
      "✅ Approve",
      "❌ Deny",
      "💬 Reply",
      "😴 ▾",
      "🧠 Always…",
    ]);
    expect(auditVerbs(store)).toContain("escalation.enqueued");
  });

  test("an attention.question escalation loses approve/deny", () => {
    const seeded = seedEscalation(store, { eventType: "attention.question" });
    channel.sendEscalation({
      escalationId: seeded.escalationId,
      incidentId: seeded.incidentId,
      carSessionId: seeded.carSessionId,
      severity: "attention",
      question: "which branch should I target?",
      contextLines: [],
    });
    expect(outboxRows(store)[0]!.body.inline_keyboard!.flat().map((b) => b.text)).toEqual([
      "💬 Reply",
      "😴 ▾",
      "🧠 Always…",
    ]);
  });

  test("a muted session is held for the digest, but urgent still breaks through", () => {
    const seeded = seedEscalation(store);
    store.db
      .query("UPDATE sessions SET muted_until = ? WHERE car_session_id = ?")
      .run(new Date(clock.current.getTime() + 3_600_000).toISOString(), seeded.carSessionId);

    const msg = {
      escalationId: seeded.escalationId,
      incidentId: seeded.incidentId,
      carSessionId: seeded.carSessionId,
      question: "q",
      contextLines: [],
    };
    channel.sendEscalation({ ...msg, severity: "attention" });
    channel.sendEscalation({ ...msg, severity: "urgent", notificationRevision: "urgent-upgrade" });

    const rows = outboxRows(store);
    expect(rows[0]!.target.queue_for_digest).toBe(true);
    expect(rows[1]!.target.queue_for_digest).toBeUndefined();
  });

  test("an upstream queue_for_digest flag is respected as-is", () => {
    const seeded = seedEscalation(store);
    channel.sendEscalation({
      escalationId: seeded.escalationId,
      incidentId: seeded.incidentId,
      carSessionId: seeded.carSessionId,
      severity: "notice",
      question: "q",
      contextLines: [],
      queue_for_digest: true,
    } as never);
    expect(outboxRows(store)[0]!.target.queue_for_digest).toBe(true);
  });

  test("sendNotify and sendDigest enqueue too", () => {
    const seeded = seedEscalation(store);
    channel.sendNotify("heads up", seeded.carSessionId);
    channel.sendDigest("☀️ CAR digest — Tue Aug 26\n🤖 Handled (0)");

    const rows = outboxRows(store);
    expect(rows[0]!.target).toMatchObject({ kind: "notify", car_session_id: seeded.carSessionId });
    expect(rows[1]!.target.kind).toBe("digest");
    expect(rows[1]!.body.text).toContain("☀️ CAR digest");
  });

  test("the digest ignores mutes — silence is never ambiguous", () => {
    const seeded = seedEscalation(store);
    store.db
      .query("UPDATE sessions SET muted_until = ? WHERE car_session_id = ?")
      .run(new Date(clock.current.getTime() + 3_600_000).toISOString(), seeded.carSessionId);
    channel.sendDigest("☀️ digest");
    expect(outboxRows(store)[0]!.target.queue_for_digest).toBeUndefined();
  });
});

describe("grammY isolation", () => {
  test("grammY is imported by exactly one module, and only lazily", async () => {
    // Structural guarantee: the whole surface is reachable in tests without a
    // Bot ever being constructed, because nothing on the import path names grammY.
    const dir = new URL("../../src/surfaces/telegram/", import.meta.url).pathname;
    const files = [...new Bun.Glob("*.ts").scanSync(dir)].sort();
    const importers: string[] = [];
    for (const file of files) {
      const src = await Bun.file(`${dir}${file}`).text();
      if (/from\s+["']grammy["']/.test(src)) importers.push(file);
    }
    expect(importers).toEqual(["bot.ts"]);

    // …and index.ts only reaches bot.ts through a dynamic import inside start().
    const index = await Bun.file(`${dir}index.ts`).text();
    expect(/^import .*["']\.\/bot\.ts["']/m.test(index)).toBe(false);
    expect(index).toContain('await import("./bot.ts")');
    expect(channel.loop.name).toBe("telegram");
  });

  test("loop.start is a no-op when Telegram is disabled", async () => {
    await channel.loop.start();
    await channel.loop.stop();
    expect(auditVerbs(store)).not.toContain("telegram.started");
  });

  test("loop.start refuses to poll without a token and says why", async () => {
    const withEnabled = build({ enabled: true, token_env: "CAR_TEST_TOKEN_ABSENT" });
    delete process.env.CAR_TEST_TOKEN_ABSENT;
    await withEnabled.loop.start();
    await withEnabled.loop.stop();
    expect(auditVerbs(store)).toContain("telegram.disabled");
    expect(auditVerbs(store)).not.toContain("telegram.started");
  });

  test("deliverOnce is inert without a transport, and works with an injected one", async () => {
    channel.sendNotify("hello");
    expect(await channel.deliverOnce()).toEqual({ sent: 0, retried: 0, dead: 0, deferred: 0 });
    expect(outboxRows(store)[0]!.state).toBe("pending");

    const send = new FakeSend();
    expect((await channel.deliverOnce(send.fn)).sent).toBe(1);
    expect(send.calls).toHaveLength(1);
  });
});
