import { describe, expect, test } from "bun:test";
import { memoryStore, FakeClock } from "./fakes.ts";
import { parseEvent, CONTRACT_VERSION } from "../src/contract/events.ts";

function ev(overrides: Record<string, unknown> = {}) {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: "agentctl:exec-1:terminal",
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "agentctl", host: "mac", adapter: "subscribe-webhook" },
    session: { vendor: "codex", native_id: "uuid-1", host: "mac", repo: "github.com/x/y" },
    type: "attention.question",
    severity: "attention",
    requires_response: true,
    title: "Which migration strategy?",
    ...overrides,
  });
}

describe("store", () => {
  test("ingest is idempotent on idempotency_key", () => {
    const store = memoryStore();
    const first = store.ingestEvent(ev());
    const second = store.ingestEvent(ev());
    expect(first.inserted).toBe(true);
    expect(second.inserted).toBe(false);
    expect(second.event_id).toBe(first.event_id);
    const n = (store.db.query("SELECT COUNT(*) n FROM events").get() as { n: number }).n;
    expect(n).toBe(1);
  });

  test("same session ref maps to one car session; distinct refs can be linked", () => {
    const store = memoryStore();
    const a = store.ingestEvent(ev());
    const b = store.ingestEvent(ev({ idempotency_key: "agentctl:exec-1:artifact", type: "artifact" }));
    expect(a.car_session_id).toBe(b.car_session_id);

    store.linkSessionRef(a.car_session_id!, { vendor: "agentctl", host: "mac", native_id: "exec-1" });
    const c = store.ingestEvent(
      ev({
        idempotency_key: "agentctl:exec-1:heartbeat",
        type: "heartbeat",
        session: { vendor: "agentctl", native_id: "exec-1", host: "mac" },
      }),
    );
    expect(c.car_session_id).toBe(a.car_session_id);
  });

  test("claim leases events and reclaims expired leases", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    store.ingestEvent(ev());
    const claimed = store.claimPendingEvents(10, 120);
    expect(claimed.length).toBe(1);
    // still leased: nothing to claim
    expect(store.claimPendingEvents(10, 120).length).toBe(0);
    // lease expires: reclaimable
    clock.advance(121 * 1000);
    expect(store.claimPendingEvents(10, 120).length).toBe(1);
  });

  test("every ingest writes audit", () => {
    const store = memoryStore();
    store.ingestEvent(ev());
    const n = (store.db.query("SELECT COUNT(*) n FROM audit WHERE verb = 'event.ingested'").get() as { n: number }).n;
    expect(n).toBe(1);
  });

  test("spend accumulates by day/provider/model", () => {
    const store = memoryStore();
    store.recordSpend("anthropic", "claude-haiku-4-5", 100, 50, 0.01);
    store.recordSpend("anthropic", "claude-haiku-4-5", 200, 80, 0.02);
    const today = store.spendToday();
    expect(today.calls).toBe(2);
    expect(today.cost_usd).toBeCloseTo(0.03);
  });
});
