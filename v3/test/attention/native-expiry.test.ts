import { afterEach, describe, expect, test } from "bun:test";
import { AttentionService } from "../../src/attention/service.ts";
import { NativeCard } from "../../src/surfaces/web/decision_views.tsx";
import { DecisionPacket, type ClientIdentity } from "../../src/attention/contract.ts";
import { deliverReply, reconcileReply } from "../../src/attention/replies.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { escalationId, incidentId } from "../../src/contract/ids.ts";
import { FakeActionBus, FakeChannel, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import { renderToString } from "hono/jsx/dom/server";
import { jsx } from "hono/jsx/jsx-runtime";

const workspace = "native-expiry";
const owner: ClientIdentity = { workspaceId: workspace, clientId: "mac", host: "mac-a" };
const stores: Store[] = [];
afterEach(() => { for (const store of stores.splice(0)) store.db.close(); });

function fixture() {
  const clock = new FakeClock();
  const store = memoryStore(clock);
  stores.push(store);
  const config = testConfig({ attention: { workspace_id: workspace } });
  const service = new AttentionService(store, config, new FakeChannel());
  return { clock, store, service };
}

function nativeEvent(clock: FakeClock, key: string, expiresAt: string) {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: key,
    ts: clock.now().toISOString(),
    source: { vendor: "codex", host: "mac", adapter: "test" },
    session: { vendor: "codex", native_id: "native-expiry", host: "mac" },
    type: "attention.question",
    severity: "attention",
    requires_response: true,
    response_channel: { kind: "codex-exec-resume", hint: { thread_id: "native-expiry" } },
    title: "Proceed?",
    body: "A response is required.",
    payload: {},
    expires_at: expiresAt,
  });
}

function incident(store: Store, openedByEvent: string, id = incidentId()): string {
  const now = store.clock.now().toISOString();
  store.db.query(`INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, dedupe_class, opened_at)
    VALUES (?, NULL, ?, 'escalated', '', 'native-expiry', ?)`).run(id, openedByEvent, now);
  return id;
}

function escalation(store: Store, eventId: string, incidentIdValue: string, id = escalationId()): string {
  store.db.query(`INSERT INTO escalations (id, incident_id, severity, question, state, created_at, origin_event_id)
    VALUES (?, ?, 'attention', 'Proceed?', 'pending', ?, ?)`).run(id, incidentIdValue, store.clock.now().toISOString(), eventId);
  return id;
}

function reply(store: Store, eventId: string, incidentIdValue: string, escalationIdValue: string, state: string, id: string) {
  const now = store.clock.now().toISOString();
  store.db.query(`INSERT INTO human_replies (id, escalation_id, incident_id, event_id, car_session_id,
      response_channel_json, payload_json, actor, state, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, 'human:test', ?, ?, ?)`).run(
    id, escalationIdValue, incidentIdValue, eventId, "sess_native_expiry",
    JSON.stringify({ kind: "codex-exec-resume", hint: { thread_id: "native-expiry" } }),
    JSON.stringify({ text: "approve" }), state, now, now,
  );
}

function seedNative(f: ReturnType<typeof fixture>, key = "native", replyState = "pending") {
  const expiresAt = new Date(f.clock.now().getTime() + 1_000).toISOString();
  const inserted = f.store.ingestEvent(nativeEvent(f.clock, key, expiresAt), { sourceId: "source:native" });
  const inc = incident(f.store, inserted.event_id);
  f.store.db.query("UPDATE events SET incident_id=?, triage_state='escalated', route_state='escalated' WHERE id=?")
    .run(inc, inserted.event_id);
  const esc = escalation(f.store, inserted.event_id, inc);
  const replyId = `reply_${key}`;
  reply(f.store, inserted.event_id, inc, esc, replyState, replyId);
  return { eventId: inserted.event_id, incidentId: inc, escalationId: esc, replyId };
}

describe("native obligation expiry", () => {
  test("expires durably once and prevents a late pending reply send", async () => {
    const f = fixture();
    const seeded = seedNative(f);
    f.clock.advance(1_001);
    f.service.sweep();

    expect(f.store.getEvent(seeded.eventId)?.obligation_state).toBe("expired");
    expect((f.store.db.query("SELECT state, revision, last_error FROM human_replies WHERE id=?").get(seeded.replyId) as { state: string; revision: number; last_error: string }).state).toBe("expired");
    expect((f.store.db.query("SELECT state FROM escalations WHERE id=?").get(seeded.escalationId) as { state: string }).state).toBe("expired");
    expect((f.store.db.query("SELECT state FROM incidents WHERE id=?").get(seeded.incidentId) as { state: string }).state).toBe("expired");
    expect((f.store.db.query("SELECT COUNT(*) AS n FROM audit WHERE verb='obligation.expired' AND object_id=?").get(seeded.eventId) as { n: number }).n).toBe(1);
    const revision = (f.store.db.query("SELECT revision FROM human_replies WHERE id=?").get(seeded.replyId) as { revision: number }).revision;

    f.service.sweep();
    expect((f.store.db.query("SELECT revision FROM human_replies WHERE id=?").get(seeded.replyId) as { revision: number }).revision).toBe(revision);
    expect((f.store.db.query("SELECT COUNT(*) AS n FROM audit WHERE verb='obligation.expired' AND object_id=?").get(seeded.eventId) as { n: number }).n).toBe(1);
    const actions = new FakeActionBus();
    await deliverReply(f.store, actions, seeded.replyId);
    expect(actions.delivered).toHaveLength(0);
    expect((f.store.db.query("SELECT state FROM human_replies WHERE id=?").get(seeded.replyId) as { state: string }).state).toBe("expired");
  });

  test("preserves unknown remote outcome when delivery was in flight", () => {
    const f = fixture();
    const seeded = seedNative(f, "in-flight", "delivering");
    f.clock.advance(1_001);
    f.service.sweep();
    const row = f.store.db.query("SELECT state, last_error FROM human_replies WHERE id=?").get(seeded.replyId) as { state: string; last_error: string };
    expect(row.state).toBe("uncertain");
    expect(row.last_error).toContain("remote acceptance is unknown");
    expect(f.store.getEvent(seeded.eventId)?.obligation_state).toBe("expired");
  });

  test("does not reconcile a reply after its native obligation expires", () => {
    const f = fixture();
    const seeded = seedNative(f, "delivered-before-expiry", "delivered");
    f.clock.advance(1_001);
    f.service.sweep();
    for (const outcome of ["source_confirmed", "not_received_retry", "cancel"] as const) {
      expect(() => reconcileReply(f.store, {
        id: seeded.replyId, expectedRevision: 1, actor: "human:test", outcome, note: "Checked the source.",
      })).toThrow("closed or expired");
    }
    expect((f.store.db.query("SELECT state FROM human_replies WHERE id=?").get(seeded.replyId) as { state: string }).state).toBe("delivered");
    expect(f.store.getEvent(seeded.eventId)?.obligation_state).toBe("expired");
  });

  test("does not close an incident while a sibling native obligation remains", () => {
    const f = fixture();
    const first = seedNative(f, "first", "pending");
    const future = f.store.ingestEvent(nativeEvent(f.clock, "sibling", new Date(f.clock.now().getTime() + 60_000).toISOString()), { sourceId: "source:native" });
    f.store.db.query("UPDATE events SET incident_id=?, triage_state='escalated', route_state='escalated' WHERE id=?")
      .run(first.incidentId, future.event_id);
    const siblingEsc = escalation(f.store, future.event_id, first.incidentId);
    f.clock.advance(1_001);
    f.service.sweep();
    expect(f.store.getEvent(first.eventId)?.obligation_state).toBe("expired");
    expect(f.store.getEvent(future.event_id)?.obligation_state).not.toBe("expired");
    expect((f.store.db.query("SELECT state FROM escalations WHERE id=?").get(siblingEsc) as { state: string }).state).toBe("pending");
    expect((f.store.db.query("SELECT state FROM incidents WHERE id=?").get(first.incidentId) as { state: string }).state).toBe("escalated");
  });

  test("guided request expiry is handled by its request row, not native sweep", () => {
    const f = fixture();
    const packet = DecisionPacket.parse({
      goal: "Ship migration", blocker: "Compatibility undecided", question: "Preserve v1?",
      why_human: "This is a new breaking-change decision", attempts: ["Checked callers"],
      facts: [{ statement: "One external caller remains", source: "src/client.ts:10" }],
      impact: "Blocks release", recommendation: { answer: "Keep v1 this release", rationale: "Avoid breaking a known caller" },
      options: [{ id: "keep", label: "Keep v1", answer: "Keep v1 this release", consequences: "One more compatibility release" }],
      deadline_at: new Date(f.clock.now().getTime() + 1_000).toISOString(),
    });
    const request = f.service.raise(owner, "guided", packet);
    f.service.publish(request.id);
    const eventId = f.service.get(request.id)!.event_id!;
    f.clock.advance(1_001);
    f.service.sweep();
    expect(f.service.get(request.id)?.state).toBe("expired");
    expect((f.store.db.query("SELECT COUNT(*) AS n FROM audit WHERE verb='obligation.expired' AND object_id=?").get(eventId) as { n: number }).n).toBe(0);
    expect((f.store.db.query("SELECT COUNT(*) AS n FROM audit WHERE verb='request.expired' AND object_id=?").get(request.id) as { n: number }).n).toBe(1);
  });

  test("native UI prioritizes an expired obligation over a prior delivery status", () => {
    const html = renderToString(jsx(NativeCard, { canWrite: false, row: {
      event_type: "attention.question", id: "esc_ui", incident_id: "inc_ui", question: "Proceed?",
      severity: "attention", state: "pending", body: "A response is required.", title: "Proceed?",
      source_host: "mac", created_at: "2026-08-26T12:00:00.000Z", obligation_state: "expired",
      reply_state: "delivered", last_error: null, snooze_until: null, reply_id: "reply_ui", reply_revision: 2,
    } }));
    expect(html).toContain("Deadline missed · not approved");
    expect(html).toContain("Delivery record: Delivered · awaiting clearance");
    expect(html).not.toContain("class=\"badge delivered\"");
    expect(html).not.toContain("Check delivery at the source");
  });
});
