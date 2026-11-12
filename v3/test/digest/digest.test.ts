import { beforeEach, describe, expect, test } from "bun:test";
import { buildDigest, fallbackOffers } from "../../src/digest/build.ts";
import { isQuietDay, renderDigest } from "../../src/digest/render.ts";
import { runWatchdog } from "../../src/digest/watchdog.ts";
import { stripButtons, extractButtons } from "../../src/surfaces/telegram/render.ts";
import { CONTRACT_VERSION, type CarEvent } from "../../src/contract/events.ts";
import {
  backdateEvent,
  makeDeps,
  seedAnsweredEscalation,
  seedHandledDecision,
  seedSession,
  setHeartbeat,
  type TestDeps,
} from "./helpers.ts";

let deps: TestDeps;

beforeEach(() => {
  deps = makeDeps();
});

function seedBusyDay(): { decisionId: string } {
  const { store } = deps;
  const dec = seedHandledDecision(store, { rationale: "approved dep bump", decidedBy: "rules" });
  seedHandledDecision(store, { rationale: "restarted forgejo", decidedBy: "llm" });
  seedAnsweredEscalation(store, { question: "force-push to fix/telemetry-cliff?", approval: false });
  seedAnsweredEscalation(store, { question: "answered hermes planning q", approval: true });

  store.recordSpend("anthropic", "claude-haiku-4-5", 3400, 900, 0.41);

  const costEvent: CarEvent = {
    contract: CONTRACT_VERSION,
    idempotency_key: "cost:1",
    ts: store.clock.now().toISOString(),
    source: { vendor: "agentctl", host: "mac-studio", adapter: "webhook" },
    session: null,
    type: "cost.report",
    severity: "info",
    requires_response: false,
    response_channel: null,
    title: "agent spend",
    body: "",
    payload: { cost_usd: 12.3 },
  };
  store.ingestEvent(costEvent);

  const now = store.clock.now().toISOString();
  store.db
    .query(
      `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, evidence_confirm, created_at, updated_at)
       VALUES ('mem_p','rule','{}','preference','{"match":"x"}','none','pending','triage',0,?,?)`,
    )
    .run(now, now);
  store.db
    .query(
      `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, evidence_confirm, created_at, updated_at)
       VALUES ('mem_o','rule','{"repo":"github.com/x/omi-desktop"}','autonomy','{"question":"approve dep bumps"}','suggest','active','outcome',5,?,?)`,
    )
    .run(now, now);
  // A 4-long confirmed streak is what makes it offer-worthy to the memory module.
  for (let i = 0; i < 4; i++) {
    store.audit("outcome", "memory.outcome_linked", "memory", "mem_o", { verdict: "confirmed" });
  }

  return { decisionId: dec };
}

describe("buildDigest", () => {
  test("collects every DESIGN §7 section from a seeded store", async () => {
    const { decisionId } = seedBusyDay();
    const session = seedSession(deps.store, { title: "multica autopilot #12" });
    setHeartbeat(deps.store, session.carSessionId, { expectedSeconds: 3600, secondsAgo: 26 * 3600 });
    const stuck = runWatchdog(deps.store, deps.config).stuck;

    const data = await buildDigest(deps.store, deps.config, { stuck });

    expect(data.handled.map((h) => h.label)).toEqual([
      "approved dep bump",
      "restarted forgejo",
    ]);
    expect(data.handled[0]!.decisionId).toBe(decisionId);
    expect(data.resolved.map((r) => r.label)).toEqual([
      "denied — force-push to fix/telemetry-cliff?",
      "approved — answered hermes planning q",
    ]);
    expect(data.stuck).toHaveLength(1);
    expect(data.spend.triageUsd).toBeCloseTo(0.41, 5);
    expect(data.spend.triageCalls).toBe(1);
    expect(data.spend.agentsUsd).toBeCloseTo(12.3, 5);
    expect(data.spend.byModel[0]!.model).toBe("claude-haiku-4-5");
    expect(data.memory.pending).toBe(1);
    expect(data.memory.offers).toHaveLength(1);
    expect(data.memory.offers[0]!.memoryId).toBe("mem_o");
    expect(isQuietDay(data)).toBe(false);
  });

  test("human decisions are not counted as handled autonomously", async () => {
    seedHandledDecision(deps.store, { decidedBy: "human", rationale: "david did it" });
    const data = await buildDigest(deps.store, deps.config);
    expect(data.handled).toHaveLength(0);
  });

  test("only the window is included", async () => {
    seedHandledDecision(deps.store, { rationale: "old news" });
    deps.store.db.query("UPDATE decisions SET created_at = '2020-01-01T00:00:00Z'").run();
    const data = await buildDigest(deps.store, deps.config);
    expect(data.handled).toHaveLength(0);
  });

  test("deferred outbox rows surface as held items", async () => {
    deps.store.enqueueOutbox("telegram", { kind: "notify" }, { text: "quiet-hours notice" });
    deps.store.db.query("UPDATE outbox SET state = 'deferred'").run();
    const data = await buildDigest(deps.store, deps.config);
    expect(data.held).toHaveLength(1);
    expect(data.held[0]!.label).toBe("quiet-hours notice");
  });

  test("escalate-only mode is surfaced", async () => {
    deps.store.kvSet("escalate_only", true);
    const data = await buildDigest(deps.store, deps.config);
    expect(data.escalateOnly).toBe(true);
  });

  test("promotion offers come from the memory module when it exposes them", async () => {
    // Integration with WS-C's real `pendingPromotionOffers`: a suggest-tier rule
    // with a 4-long confirmed streak. The digest must surface it verbatim.
    const now = deps.store.clock.now().toISOString();
    deps.store.db
      .query(
        `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, created_at, updated_at)
         VALUES ('mem_real','rule','{"repo":"github.com/x/omi-desktop"}','autonomy','{"match":"dep_bump","disposition":"auto_resolve"}','suggest','active','outcome',?,?)`,
      )
      .run(now, now);
    for (let i = 0; i < 4; i++) {
      deps.store.audit("outcome", "memory.outcome_linked", "memory", "mem_real", { verdict: "confirmed" });
    }

    const data = await buildDigest(deps.store, deps.config);
    expect(data.memory.offers.map((o) => o.memoryId)).toEqual(["mem_real"]);
    expect(data.memory.offers[0]!.label.length).toBeGreaterThan(0);
  });

  test("an override resets the streak, so nothing is offered", async () => {
    const now = deps.store.clock.now().toISOString();
    deps.store.db
      .query(
        `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, created_at, updated_at)
         VALUES ('mem_real','rule','{}','autonomy','{"match":"dep_bump"}','suggest','active','outcome',?,?)`,
      )
      .run(now, now);
    for (let i = 0; i < 4; i++) {
      deps.store.audit("outcome", "memory.outcome_linked", "memory", "mem_real", { verdict: "confirmed" });
    }
    deps.store.audit("outcome", "memory.outcome_linked", "memory", "mem_real", { verdict: "overridden" });

    const data = await buildDigest(deps.store, deps.config);
    expect(data.memory.offers).toHaveLength(0);
  });

  test("the standalone fallback covers a memory module with no promotion API", () => {
    // Only reachable if src/memory stops exporting pendingPromotionOffers;
    // DESIGN §6's rule (N confirmations, zero overrides) implemented locally.
    const now = deps.store.clock.now().toISOString();
    const insert = (id: string, confirms: number, overrides: number) =>
      deps.store.db
        .query(
          `INSERT INTO memories (id, tier, scope_json, kind, content_json, autonomy, status, authored_by, evidence_confirm, evidence_override, created_at, updated_at)
           VALUES (?, 'rule','{"repo":"github.com/x/omi-desktop"}','autonomy','{"match":"dep_bump"}','suggest','active','outcome',?,?,?,?)`,
        )
        .run(id, confirms, overrides, now, now);

    insert("mem_ready", 4, 0);
    insert("mem_short", 3, 0);
    insert("mem_burned", 9, 1);

    const offers = fallbackOffers(deps.store);
    expect(offers.map((o) => o.memoryId)).toEqual(["mem_ready"]);
    expect(offers[0]!.label).toContain("omi-desktop");
    expect(offers[0]!.label).toContain("matched you 4×");
  });
});

describe("renderDigest", () => {
  test("renders every section with counts and buttons", async () => {
    seedBusyDay();
    const session = seedSession(deps.store, { title: "multica autopilot #12", requiresResponse: true });
    backdateEvent(deps.store, session.eventId, 26);
    const stuck = runWatchdog(deps.store, deps.config).stuck;

    const data = await buildDigest(deps.store, deps.config, { stuck });
    const md = renderDigest(data, deps.clock.current);
    const text = stripButtons(md);

    expect(text.split("\n")[0]).toBe("☀️ CAR digest — Wed Aug 26");
    expect(text).toContain("🤖 Handled (2)");
    expect(text).toContain("• approved dep bump");
    expect(text).toContain("🙋 You resolved (2)");
    expect(text).toContain("• denied — force-push to fix/telemetry-cliff?");
    expect(text).toContain("⚠️ Stuck / silent (1)");
    expect(text).toContain("multica autopilot #12");
    expect(text).toContain("💸 Spend: triage $0.41 (1 runs) · agents ~$12.30 (reported)");
    expect(text).toContain("claude-haiku-4-5 $0.41 (1)");
    expect(text).toContain("🧠 Memory: 1 pending learning");
    expect(text).toContain("1 promotion offer");
    expect(text).toContain('• Auto-handle "approve dep bumps" from now on?');
    expect(text).not.toContain("Quiet day");

    const buttons = extractButtons(md);
    const dataStrings = buttons.map((b) => b.data);
    expect(dataStrings.filter((d) => d.startsWith("du:"))).toHaveLength(2);
    expect(dataStrings.filter((d) => d.startsWith("dd:"))).toHaveLength(2);
    expect(dataStrings.some((d) => d.startsWith("pb:"))).toBe(true);
    expect(dataStrings.some((d) => d.startsWith("es:"))).toBe(true);
    expect(dataStrings).toContain("pmy:mem_o");
    expect(dataStrings).toContain("pmr:mem_o");
    expect(dataStrings).toContain("pmk:mem_o");
    expect(dataStrings).toContain("mr:pending");
  });

  test("a day where nothing happened says so explicitly — never skipped", async () => {
    const data = await buildDigest(deps.store, deps.config);
    expect(isQuietDay(data)).toBe(true);

    const text = stripButtons(renderDigest(data, deps.clock.current));
    expect(text).toContain("🤖 Handled (0)");
    expect(text).toContain("• nothing — CAR took no autonomous action.");
    expect(text).toContain("🙋 You resolved (0)");
    expect(text).toContain("⚠️ Stuck / silent (0)");
    expect(text).toContain("💸 Spend: triage $0.00 (0 runs)");
    expect(text).toContain("🧠 Memory: 0 pending learnings");
    expect(text).toContain("🌙 Quiet day: no agent activity was recorded at all.");
    expect(text).toContain("silence is never ambiguous");
  });

  test("escalate-only mode is called out at the top", async () => {
    deps.store.kvSet("escalate_only", true);
    const data = await buildDigest(deps.store, deps.config);
    expect(renderDigest(data, deps.clock.current)).toContain("🛑 ESCALATE-ONLY mode is on");
  });

  test("missed days are named in the digest", async () => {
    const data = await buildDigest(deps.store, deps.config, { missedDays: ["2026-08-24", "2026-08-25"] });
    const text = renderDigest(data, deps.clock.current);
    expect(text).toContain("No digest was produced for 2026-08-24");
    expect(text).toContain("No digest was produced for 2026-08-25");
  });
});
