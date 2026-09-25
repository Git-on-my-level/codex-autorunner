import { afterEach, describe, expect, test } from "bun:test";
import { recordAnswer } from "../../src/attention/replies.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { buildDeps, mountApp, seedEscalation, seedIncident, WEB_AUTH_HEADERS } from "./helpers.ts";

const stores: ReturnType<typeof memoryStore>[] = [];
afterEach(() => { for (const store of stores.splice(0)) store.db.close(); });

function fixture() {
  const clock = new FakeClock();
  const store = memoryStore(clock);
  stores.push(store);
  const deps = buildDeps({ store });
  return { app: mountApp(deps), clock, store };
}

function nativeFixture() {
  const f = fixture();
  const event = f.store.ingestEvent(parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: `web-error-${Math.random()}`,
    ts: f.clock.now().toISOString(),
    source: { vendor: "other", host: "native-host", adapter: "web-error-test" },
    session: { vendor: "other", native_id: "native-session", host: "native-host" },
    type: "attention.question", severity: "attention", requires_response: true,
    title: "Native question", body: "A source needs a human answer.",
  }));
  const incident = seedIncident(f.store.db, f.clock, { opened_by_event: event.event_id, state: "escalated" });
  f.store.db.query("UPDATE events SET incident_id=? WHERE id=?").run(incident, event.event_id);
  const escalation = seedEscalation(f.store.db, f.clock, incident, { question: "Native question" });
  return { ...f, escalation };
}

describe("web action error recovery", () => {
  test("keeps a native validation failure on the current Needs you card", async () => {
    const f = nativeFixture();
    const response = await f.app.request(`/ui/escalations/${f.escalation}/answer`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "" }),
    });
    expect(response.status).toBe(400);
    const html = await response.text();
    expect(html).toContain("Action not confirmed");
    expect(html).toContain(`/ui?selected=native%3A${f.escalation}`);
  });

  test("retains a failed native answer draft and links back to that card", async () => {
    const f = nativeFixture();
    recordAnswer(f.store, { escalationId: f.escalation, actor: "human:web", payload: { text: "Already recorded" } });
    const response = await f.app.request(`/ui/escalations/${f.escalation}/answer`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "Keep this native draft" }),
    });
    expect(response.status).toBe(409);
    const html = await response.text();
    expect(html).toContain("Keep this native draft");
    expect(html).toContain("Retained here for copying");
    expect(html).toContain(`/ui/watching?selected=native%3A${f.escalation}`);
  });

  test("retains a stale reconciliation note and links to its delivery record", async () => {
    const f = fixture();
    const reply = f.store.db.query("INSERT INTO human_replies (id, payload_json, actor, state, created_at, updated_at) VALUES (?, ?, ?, 'uncertain', ?, ?) RETURNING id").get(
      "reply_web_error", JSON.stringify({ text: "Source answer" }), "human:web", f.clock.now().toISOString(), f.clock.now().toISOString(),
    ) as { id: string };
    const response = await f.app.request(`/ui/replies/${reply.id}/reconcile`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ expected_revision: "999", outcome: "source_confirmed", note: "Keep this source-check note" }),
    });
    expect(response.status).toBe(409);
    const html = await response.text();
    expect(html).toContain("Keep this source-check note");
    expect(html).toContain("Retained here for copying");
    expect(html).toContain(`/ui/watching?selected=delivery%3A${reply.id}`);
  });
});
