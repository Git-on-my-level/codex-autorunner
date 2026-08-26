import { beforeEach, describe, expect, test } from "bun:test";
import { FakeActionBus, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import {
  handleCallback,
  handleCommand,
  routeIncomingMessage,
  updateTicker,
  type HandlerDeps,
} from "../../src/surfaces/telegram/handlers.ts";
import { encodeCallback, CB } from "../../src/surfaces/telegram/render.ts";
import { FakeMemoryWriter, auditVerbs, markDelivered, outboxRows, seedEscalation } from "./helpers.ts";

let clock: FakeClock;
let store: Store;
let actions: FakeActionBus;
let memory: FakeMemoryWriter;
let deps: HandlerDeps;

beforeEach(() => {
  clock = new FakeClock();
  store = memoryStore(clock);
  actions = new FakeActionBus();
  memory = new FakeMemoryWriter();
  deps = {
    store,
    actions,
    memoryWriter: memory,
    host: "mac-studio",
    config: testConfig({ telegram: { enabled: true, chat_id: "-100", digest_time: "08:30" } }),
  };
});

const tap = (op: string, id: string, messageText?: string) =>
  handleCallback(deps, {
    data: `${op}:${id}`,
    from: "david",
    messageId: "555",
    ...(messageText ? { messageText } : {}),
  });

describe("approve / deny", () => {
  test("approve writes the answer, delivers, records the outcome and edits in place", async () => {
    const seeded = seedEscalation(store, { suggested: { approval: true, label: "APPROVE" } });
    markDelivered(store, seeded);

    const ack = await tap(CB.approve, seeded.escalationId, "🔴 needs you · claude-code\nforce push?");
    expect(ack.text).toBe("✅ approved");

    const esc = store.db.query("SELECT * FROM escalations WHERE id = ?").get(seeded.escalationId) as {
      state: string;
      answered_by: string;
      answer_json: string;
      answered_at: string;
    };
    expect(esc.state).toBe("answered");
    expect(esc.answered_by).toBe("david");
    expect(JSON.parse(esc.answer_json)).toEqual({ approval: true, by: "david" });
    expect(esc.answered_at).toBe(clock.current.toISOString());

    const inc = store.db.query("SELECT state, closed_at FROM incidents WHERE id = ?").get(seeded.incidentId) as {
      state: string;
      closed_at: string;
    };
    expect(inc.state).toBe("resolved");

    // Delivered through the action bus with the session's response channel.
    expect(actions.delivered).toEqual([{ carSessionId: seeded.carSessionId, payload: { approval: true } }]);

    // The tap IS the learning signal — it matched the suggestion.
    expect(memory.outcomes).toEqual([
      {
        decisionId: seeded.decisionId,
        escalationId: seeded.escalationId,
        verdict: "confirmed",
        davidAction: { approval: true },
      },
    ]);

    // Edit-in-place is an outbox row, not an API call.
    const edit = outboxRows(store).find((r) => r.target.kind === "edit");
    expect(edit).toBeDefined();
    expect(edit!.target.edit_message_id).toBe("555");
    expect(edit!.body.text).toContain("— ✅ approved (david)");
    expect(edit!.body.inline_keyboard).toBeUndefined();
  });

  test("a tap against the suggestion is recorded as an override", async () => {
    const seeded = seedEscalation(store, { suggested: { approval: false, label: "DENY" } });
    markDelivered(store, seeded);
    await tap(CB.approve, seeded.escalationId);
    expect(memory.outcomes[0]!.verdict).toBe("overridden");
  });

  test("no suggestion means the outcome is neither confirm nor override", async () => {
    const seeded = seedEscalation(store, { suggested: null });
    markDelivered(store, seeded);
    await tap(CB.deny, seeded.escalationId);
    expect(memory.outcomes[0]!.verdict).toBe("flagged");
  });

  test("double taps are idempotent", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    await tap(CB.deny, seeded.escalationId);
    const ack = await tap(CB.approve, seeded.escalationId);
    expect(ack.text).toBe("Already answered.");
    expect(actions.delivered).toHaveLength(1);
    expect(memory.outcomes).toHaveLength(1);
  });

  test("a failed delivery reopens the incident and says so loudly", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    actions.deliverResult = "failed";

    const ack = await tap(CB.deny, seeded.escalationId);
    expect(ack.alert).toBe(true);
    expect(ack.text).toContain("FAILED");

    const inc = store.db.query("SELECT state FROM incidents WHERE id = ?").get(seeded.incidentId) as {
      state: string;
    };
    expect(inc.state).toBe("open");
    const notify = outboxRows(store).find((r) => r.body.text.includes("could NOT be delivered"));
    expect(notify).toBeDefined();
    expect(auditVerbs(store)).toContain("escalation.delivery_failed");
  });

  test("a degraded delivery is honest about being staged", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    actions.deliverResult = "degraded";
    const ack = await tap(CB.approve, seeded.escalationId);
    expect(ack.text).toContain("staged");
  });

  test("a vanished escalation does not throw", async () => {
    const ack = await tap(CB.approve, "esc_nope");
    expect(ack.alert).toBe(true);
  });
});

describe("💬 reply", () => {
  test("enqueues a force-reply prompt naming the session", async () => {
    const seeded = seedEscalation(store, { title: "fix BLE reconnect" });
    markDelivered(store, seeded);
    const ack = await tap(CB.reply, seeded.escalationId);
    expect(ack.text).toContain("Reply");
    const prompt = outboxRows(store).find((r) => r.body.force_reply === true);
    expect(prompt).toBeDefined();
    expect(prompt!.body.text).toContain("fix BLE reconnect");
    expect(prompt!.target.escalation_id).toBe(seeded.escalationId);
  });
});

describe("😴 snooze", () => {
  test("the menu tap only swaps the keyboard", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    await tap(CB.snoozeMenu, seeded.escalationId, "card text");
    const edit = outboxRows(store).find((r) => r.target.kind === "edit")!;
    expect(edit.body.text).toBe("card text");
    expect(edit.body.inline_keyboard!.flat().map((b) => b.data)).toEqual([
      `sz1:${seeded.escalationId}`,
      `szn:${seeded.escalationId}`,
      `szd:${seeded.escalationId}`,
      `bk:${seeded.escalationId}`,
    ]);
    // No state change yet.
    const inc = store.db.query("SELECT state FROM incidents WHERE id = ?").get(seeded.incidentId) as {
      state: string;
    };
    expect(inc.state).toBe("escalated");
  });

  test("1h snoozes the incident with a concrete wake time", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    const ack = await tap(CB.snooze1h, seeded.escalationId);
    expect(ack.text).toContain("in 1h");

    const inc = store.db
      .query("SELECT state, snooze_until FROM incidents WHERE id = ?")
      .get(seeded.incidentId) as { state: string; snooze_until: string };
    expect(inc.state).toBe("snoozed");
    expect(Date.parse(inc.snooze_until) - clock.current.getTime()).toBe(3_600_000);

    const esc = store.db.query("SELECT state FROM escalations WHERE id = ?").get(seeded.escalationId) as {
      state: string;
    };
    expect(esc.state).toBe("snoozed");
    expect(auditVerbs(store)).toContain("incident.snoozed");
  });

  test("next-digest snooze lands on the configured digest time", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    await tap(CB.snoozeDigest, seeded.escalationId);
    const inc = store.db.query("SELECT snooze_until FROM incidents WHERE id = ?").get(seeded.incidentId) as {
      snooze_until: string;
    };
    const until = new Date(inc.snooze_until);
    expect(until.getHours()).toBe(8);
    expect(until.getMinutes()).toBe(30);
    expect(until.getTime()).toBeGreaterThan(clock.current.getTime());
  });
});

describe("🧠 Always…", () => {
  test("only an explicit tap grants autonomy, and it creates the rule first", async () => {
    const seeded = seedEscalation(store, { repo: "github.com/x/omi-desktop", suggested: { approval: false } });
    markDelivered(store, seeded);

    // Opening the submenu grants nothing.
    await tap(CB.alwaysMenu, seeded.escalationId);
    expect(memory.autonomy).toHaveLength(0);

    const ack = await tap(CB.alwaysAll, seeded.escalationId);
    expect(ack.text).toContain("Granted");

    expect(memory.added).toHaveLength(1);
    const rule = memory.added[0]!;
    expect(rule.tier).toBe("rule");
    expect(rule.content.match).toBe("force_push");
    expect(rule.content.action_class).toBe("deny_permission");
    expect(rule.scope).toEqual({
      vendor: "claude-code",
      event_type: "attention.permission",
      dedupe_class: "force_push",
    });
    expect(memory.autonomy).toEqual([{ memoryId: rule.id, autonomy: "granted", by: "david" }]);
    expect(auditVerbs(store)).toContain("memory.autonomy_granted_from_escalation");
  });

  test("'this repo only' narrows the scope", async () => {
    const seeded = seedEscalation(store, { repo: "github.com/x/omi-desktop" });
    markDelivered(store, seeded);
    await tap(CB.alwaysRepo, seeded.escalationId);
    expect(memory.added[0]!.scope.repo).toBe("github.com/x/omi-desktop");
  });

  test("keep-asking grants nothing and restores the main keyboard", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    await tap(CB.alwaysKeep, seeded.escalationId, "card");
    expect(memory.autonomy).toHaveLength(0);
    const edit = outboxRows(store).at(-1)!;
    expect(edit.body.inline_keyboard!.flat().map((b) => b.text)).toContain("✅ Approve");
  });

  test("the escalation still needs an answer after a grant", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded);
    await tap(CB.alwaysAll, seeded.escalationId);
    const esc = store.db.query("SELECT state FROM escalations WHERE id = ?").get(seeded.escalationId) as {
      state: string;
    };
    expect(esc.state).toBe("pending");
  });
});

describe("digest feedback + promotion", () => {
  test("👍 / 👎 record outcomes against the decision", async () => {
    const seeded = seedEscalation(store);
    await handleCallback(deps, { data: encodeCallback(CB.digestUp, seeded.decisionId), from: "david" });
    await handleCallback(deps, { data: encodeCallback(CB.digestDown, seeded.decisionId), from: "david" });
    expect(memory.outcomes.map((o) => o.verdict)).toEqual(["confirmed", "overridden"]);
  });

  test("a promotion offer grants only on the Yes tap", async () => {
    const now = clock.current.toISOString();
    store.db
      .query(
        `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, created_at, updated_at)
         VALUES ('mem_1','rule','{"vendor":"claude-code"}','autonomy','{"match":"dep_bump"}','suggest','active','consolidator',?,?)`,
      )
      .run(now, now);

    expect((await handleCallback(deps, { data: "pmk:mem_1" })).text).toContain("Keeping ask-first");
    expect(memory.autonomy).toHaveLength(0);

    await handleCallback(deps, { data: "pmy:mem_1" });
    expect(memory.autonomy).toEqual([{ memoryId: "mem_1", autonomy: "granted", by: "david" }]);
  });

  test("unknown callback data is inert", async () => {
    expect((await handleCallback(deps, { data: "garbage" })).text).toBe("unknown button");
  });
});

describe("digest stuck-session buttons", () => {
  test("🔍 probe runs a read-only template and reports back into the thread", async () => {
    const seeded = seedEscalation(store);
    store.db
      .query("UPDATE sessions SET cwd = '/Users/dazheng/omi' WHERE car_session_id = ?")
      .run(seeded.carSessionId);

    const ack = await handleCallback(deps, { data: `pb:${seeded.carSessionId}` });
    expect(ack.text).toContain("probing");
    expect(actions.templates).toEqual([{ templateId: "git.status", args: { repo: "/Users/dazheng/omi" } }]);

    await Promise.resolve(); // let the detached probe settle
    await Promise.resolve();
    expect(outboxRows(store).some((r) => r.body.text.startsWith("🔍 git.status"))).toBe(true);
  });

  test("a session with no cwd falls back to an agentctl probe", async () => {
    const seeded = seedEscalation(store);
    store.db.query("UPDATE sessions SET cwd = NULL WHERE car_session_id = ?").run(seeded.carSessionId);
    await handleCallback(deps, { data: `pb:${seeded.carSessionId}` });
    expect(actions.templates[0]!.templateId).toBe("agentctl.recent");
  });

  test("escalate reopens snoozed incidents for that session", async () => {
    const seeded = seedEscalation(store);
    store.db
      .query("UPDATE incidents SET state = 'snoozed', snooze_until = '2099-01-01T00:00:00Z' WHERE id = ?")
      .run(seeded.incidentId);

    const ack = await handleCallback(deps, { data: `es:${seeded.carSessionId}` });
    expect(ack.text).toBe("Escalated.");
    const inc = store.db.query("SELECT state, snooze_until FROM incidents WHERE id = ?").get(seeded.incidentId) as {
      state: string;
      snooze_until: string | null;
    };
    expect(inc.state).toBe("open");
    expect(inc.snooze_until).toBeNull();
    expect(auditVerbs(store)).toContain("digest.escalate_requested");
  });

  test("buttons for a vanished session are inert", async () => {
    expect((await handleCallback(deps, { data: "pb:sess_gone" })).text).toBe("Session not found.");
    expect((await handleCallback(deps, { data: "es:sess_gone" })).text).toBe("Session not found.");
  });
});

describe("inbound replies", () => {
  test("a reply to an escalation card routes verbatim to the agent", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded, "900");

    const outcome = await routeIncomingMessage(deps, {
      text: "rebase instead, do not force-push",
      messageId: "901",
      replyToMessageId: "900",
    });

    expect(outcome).toEqual({
      kind: "delivered",
      carSessionId: seeded.carSessionId,
      escalationId: seeded.escalationId,
    });
    expect(actions.delivered[0]!.payload).toEqual({ text: "rebase instead, do not force-push" });

    const esc = store.db
      .query("SELECT state, answered_by, answer_json FROM escalations WHERE id = ?")
      .get(seeded.escalationId) as { state: string; answered_by: string; answer_json: string };
    expect(esc.state).toBe("answered");
    expect(esc.answered_by).toBe("david");
    expect(JSON.parse(esc.answer_json)).toEqual({ text: "rebase instead, do not force-push" });
    // Free text is a correction, never a silent confirmation.
    expect(memory.outcomes[0]!.verdict).toBe("corrected");
  });

  test("a reply in a forum topic routes by thread id", async () => {
    const seeded = seedEscalation(store);
    store.db
      .query("UPDATE sessions SET telegram_thread_id = '42' WHERE car_session_id = ?")
      .run(seeded.carSessionId);

    const outcome = await routeIncomingMessage(deps, { text: "carry on", messageId: "5", threadId: "42" });
    expect(outcome.kind).toBe("delivered");
    expect(actions.delivered[0]!.carSessionId).toBe(seeded.carSessionId);
  });

  test("an unrouteable message becomes a note event", async () => {
    const outcome = await routeIncomingMessage(deps, { text: "remember the milk", messageId: "77" });
    expect(outcome.kind).toBe("note");

    const event = store.db
      .query("SELECT type, source_adapter, body, idempotency_key FROM events WHERE idempotency_key = ?")
      .get("telegram:note:77") as { type: string; source_adapter: string; body: string };
    expect(event.type).toBe("note");
    expect(event.source_adapter).toBe("telegram");
    expect(event.body).toBe("remember the milk");
    expect(actions.delivered).toHaveLength(0);
  });

  test("a failed reply delivery is reported, never dropped", async () => {
    const seeded = seedEscalation(store);
    markDelivered(store, seeded, "900");
    actions.deliverResult = "failed";
    const outcome = await routeIncomingMessage(deps, { text: "hi", messageId: "1", replyToMessageId: "900" });
    expect(outcome.kind).toBe("failed");
    expect(outboxRows(store).some((r) => r.body.text.includes("Could not deliver"))).toBe(true);
  });

  test("empty messages are ignored", async () => {
    expect((await routeIncomingMessage(deps, { text: "   ", messageId: "2" })).kind).toBe("ignored");
  });
});

describe("commands", () => {
  test("/status counts what needs David", async () => {
    const seeded = seedEscalation(store);
    store.recordSpend("anthropic", "claude-haiku-4-5", 100, 20, 0.41);
    const out = await handleCommand(deps, { command: "/status", args: "" });
    expect(out).toContain("escalations pending: 1");
    expect(out).toContain("$0.41");
    expect(out).toContain("mode: normal");
    expect(seeded.escalationId).toBeTruthy();
  });

  test("/remember stores a David-authored note", async () => {
    const out = await handleCommand(deps, { command: "/remember", args: "never force-push omi-desktop" });
    expect(out).toContain("Remembered");
    expect(memory.added[0]).toMatchObject({
      tier: "note",
      kind: "fact",
      content: { text: "never force-push omi-desktop" },
    });
    expect(await handleCommand(deps, { command: "/remember", args: "" })).toContain("Usage");
  });

  test("/mute silences a session for a duration", async () => {
    const seeded = seedEscalation(store);
    const out = await handleCommand(deps, { command: "/mute", args: `${seeded.carSessionId} 2h` });
    expect(out).toContain("Muted");
    const row = store.db
      .query("SELECT muted_until FROM sessions WHERE car_session_id = ?")
      .get(seeded.carSessionId) as { muted_until: string };
    expect(Date.parse(row.muted_until) - clock.current.getTime()).toBe(2 * 3_600_000);

    expect(await handleCommand(deps, { command: "/mute", args: "sess_nope 2h" })).toContain("No session");
    expect(await handleCommand(deps, { command: "/mute", args: `${seeded.carSessionId} banana` })).toContain(
      "Cannot parse duration",
    );
  });

  test("/mute also resolves a session by title", async () => {
    const seeded = seedEscalation(store, { title: "fix BLE reconnect" });
    await handleCommand(deps, { command: "/mute", args: "BLE 1d" });
    const row = store.db
      .query("SELECT muted_until FROM sessions WHERE car_session_id = ?")
      .get(seeded.carSessionId) as { muted_until: string | null };
    expect(row.muted_until).not.toBeNull();
  });

  test("/panic flips escalate-only and cancels in-flight actions", async () => {
    store.db
      .query(
        "INSERT INTO actions (id, decision_id, class, policy_verdict, dedupe_hash, state) VALUES ('act_1','dec_1','reply','auto','h','running')",
      )
      .run();
    const out = await handleCommand(deps, { command: "/panic", args: "" });
    expect(out).toContain("ESCALATE-ONLY");
    // policy reads this as a strict boolean — an object here would silently disarm the breaker.
    expect(store.kvGet<boolean>("escalate_only")).toBe(true);
    expect(
      (store.db.query("SELECT state FROM actions WHERE id = 'act_1'").get() as { state: string }).state,
    ).toBe("failed");
    expect(auditVerbs(store)).toContain("panic.engaged");

    const cleared = await handleCommand(deps, { command: "/panic", args: "off" });
    expect(cleared).toContain("cleared");
    expect(store.kvGet<unknown>("escalate_only")).toBe(false);
    expect(auditVerbs(store)).toContain("panic.cleared");
  });

  test("/ticker toggles the kv flag and gates ticker updates", async () => {
    const seeded = seedEscalation(store);
    expect(updateTicker(deps, seeded.carSessionId, "running tests")).toBe(false);

    expect(await handleCommand(deps, { command: "/ticker", args: "on" })).toBe("Ticker on.");
    expect(store.kvGet<boolean>("telegram.ticker")).toBe(true);
    expect(updateTicker(deps, seeded.carSessionId, "running tests")).toBe(true);

    const row = outboxRows(store).at(-1)!;
    expect(row.target.kind).toBe("ticker");
    expect(row.body.disable_notification).toBe(true);

    expect(await handleCommand(deps, { command: "/ticker", args: "sideways" })).toContain("Usage");
  });

  test("/digest resends the latest stored digest", async () => {
    expect(await handleCommand(deps, { command: "/digest", args: "" })).toContain("No digest");
    store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES ('2026-08-25','☀️ yesterday', '2026-08-25T08:30:00Z')")
      .run();
    const out = await handleCommand(deps, { command: "/digest", args: "" });
    expect(out).toContain("2026-08-25");
    expect(outboxRows(store).at(-1)!.target.kind).toBe("digest");
  });

  test("commands are audited and unknown ones are safe", async () => {
    expect(await handleCommand(deps, { command: "/nope", args: "" })).toContain("Unknown command");
    expect(await handleCommand(deps, { command: "/help", args: "" })).toContain("/status");
    expect(await handleCommand(deps, { command: "/policy", args: "" })).toContain("mode:");
    expect(auditVerbs(store).filter((v) => v === "telegram.command")).toHaveLength(3);
  });

  test("group-suffixed commands still resolve", async () => {
    expect(await handleCommand(deps, { command: "/status@car_bot", args: "" })).toContain("CAR status");
  });
});
