import { afterEach, describe, expect, test } from "bun:test";
import { AttentionService } from "../../src/attention/service.ts";
import { DecisionPacket, type ClientIdentity } from "../../src/attention/contract.ts";
import { recordAnswer, recordSessionReply } from "../../src/attention/replies.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { buildDeps, mountApp, seedEscalation, seedIncident, WEB_AUTH_HEADERS } from "./helpers.ts";

const owner: ClientIdentity = { workspaceId: "default", clientId: "mailbox-agent", host: "mailbox-host" };
const stores: ReturnType<typeof memoryStore>[] = [];
afterEach(() => { for (const store of stores.splice(0)) store.db.close(); });

function packet(overrides: Record<string, unknown> = {}) {
  return DecisionPacket.parse({
    goal: "Ship the migration",
    blocker: "A compatibility choice is undecided",
    question: "Keep the compatibility bridge?",
    project: "Mailbox project",
    why_human: "The source cannot choose this policy safely.",
    attempts: ["Checked the callers"],
    facts: [{ statement: "One external caller still uses the old path", source: "src/client.ts" }],
    impact: "Blocks the release",
    recommendation: { answer: "Keep it for this release", rationale: "Avoid breaking the caller." },
    ...overrides,
  });
}

function fixture() {
  const clock = new FakeClock();
  const store = memoryStore(clock);
  stores.push(store);
  const deps = buildDeps({ store });
  const service = new AttentionService(store, deps.config, deps.channel);
  return { app: mountApp(deps), clock, store, service };
}

function nativeEvent() {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: `mailbox-native-${Math.random()}`,
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "other", host: "native-host", adapter: "mailbox-test" },
    session: { vendor: "other", native_id: "native-session", host: "native-host" },
    type: "attention.permission",
    severity: "urgent",
    requires_response: true,
    title: "Approve native deploy?",
    body: "The native source needs an explicit approval.",
  });
}

describe("decision mailbox routing", () => {
  test("missed decisions offer review choices without reviving expired approvals", async () => {
    const f = fixture();
    const row = f.service.raise(owner, "missed-options", packet({ deadline_at: new Date(f.clock.now().getTime()+1000).toISOString(), options: [{ id: "keep", label: "Keep bridge", answer: "Keep the bridge for this release", consequences: "Carry compatibility code" }] }));
    f.clock.advance(1000);
    const html = await (await f.app.request(`/ui/decisions/${row.id}`, { headers: WEB_AUTH_HEADERS })).text();
    expect(html).toContain("What next?");
    expect(html).toContain("Original options · no longer available");
    expect(html).toContain("Keep the bridge for this release");
    expect(html).not.toContain(`action="/ui/decisions/${row.id}/answer"`);
    expect(html).toContain('name="note" value="This decision is no longer needed."');
    const response = await f.app.request(`/ui/decisions/${row.id}/review-expiry`, { method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ expected_revision: String(row.revision), note: "This decision is no longer needed.", continue_to: "/ui" }) });
    expect(response.status).toBe(303);
    expect(f.service.get(row.id)).toMatchObject({ state: "expired", review_note: "This decision is no longer needed." });
    expect(f.service.get(row.id)!.reviewed_at).not.toBeNull();
    expect(f.store.db.query("SELECT id FROM human_replies WHERE request_id=?").get(row.id)).toBeNull();
    const inbox = await (await f.app.request("/ui")).text();
    expect(inbox).not.toContain(`aria-labelledby="question-${row.id}"`);
  });
  test("confirmation query markers cannot invent a saved outcome", async () => {
    const f = fixture();
    const active = f.service.raise(owner, "not-completed", packet());
    for (const outcome of ["answered", "withdrawn", "reviewed", "unknown"]) {
      const html = await (await f.app.request(`/ui?triage=1&completed=${outcome}&completed_id=${active.id}`)).text();
      expect(html).not.toContain('data-transient-notice="triage"');
    }
    const recorded = await (await f.app.request(`/ui/decisions/${active.id}?recorded=1`)).text();
    expect(recorded).not.toContain('data-transient-notice="recorded"');
    expect(f.service.get(active.id)!.state).toBe("needs_you");
  });
  test("sending from the inbox selects the next pending decision, including on mobile", async () => {
    const f = fixture();
    const first = f.service.raise(owner, "triage-first", packet({ question: "First decision?" }));
    f.clock.advance(1000);
    const next = f.service.raise(owner, "triage-next", packet({ question: "Next decision?" }));
    const html = await (await f.app.request(`/ui/decisions/${first.id}`, { headers: WEB_AUTH_HEADERS })).text();
    const continuation = html.match(/name="continue_to" value="([^"]+)"/)![1]!.replaceAll("&amp;", "&");
    expect(continuation).toContain(next.id);
    const response = await f.app.request(`/ui/decisions/${first.id}/answer`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ expected_revision: "1", text: "Proceed with the bridge.", continue_to: continuation }),
    });
    expect(response.status).toBe(303);
    const location = response.headers.get("location")!;
    expect(location).toStartWith("/ui?");
    const page = await (await f.app.request(location)).text();
    expect(page).toContain('class="mailbox mailbox-selected"');
    expect(page).toContain(`aria-labelledby="question-${next.id}"`);
    expect(page).not.toContain(`aria-labelledby="question-${first.id}"`);
    expect(page).toContain("Reply recorded.");
    expect(page).toContain('aria-label="View First decision?"');
    expect(page).toContain('class="notice triage-confirmation"');
    expect(page).not.toContain("The next inbox item is ready below.");
    expect(f.service.get(first.id)!.state).toBe("answered");
  });

  test("triage recovers when the next item was answered elsewhere or the page emptied", async () => {
    const f = fixture();
    const gone = f.service.raise(owner, "gone", packet());
    const remaining = f.service.raise(owner, "remaining", packet({ question: "Still waiting?" }));
    f.service.answer(gone.id, 1, "human:web", { text: "Already answered" });
    const page = await (await f.app.request(`/ui?triage=1&selected=${gone.id}&completed=answered`)).text();
    expect(page).toContain(`aria-labelledby="question-${remaining.id}"`);
    expect(page).not.toContain("This item is no longer in this mailbox");
    const emptyPage = await f.app.request("/ui?triage=1&page=9&completed=answered");
    expect(emptyPage.status).toBe(303);
    expect(emptyPage.headers.get("location")).toBe("/ui?triage=1&completed=answered");
    const attributedEmptyPage = await f.app.request(`/ui?triage=1&page=9&completed=answered&completed_id=${gone.id}`);
    expect(attributedEmptyPage.headers.get("location")).toBe(`/ui?triage=1&completed=answered&completed_id=${gone.id}`);
    f.service.answer(remaining.id, 1, "human:web", { text: "Done" });
    const empty = await (await f.app.request(`/ui?triage=1&completed=answered&completed_id=${remaining.id}`)).text();
    expect(empty).toContain("No decisions waiting here.");
    expect(empty).toContain("Reply recorded.");
  });

  test("withdrawal continues triage and supplied continuation cannot redirect off site", async () => {
    const f = fixture();
    const row = f.service.raise(owner, "withdraw-triage", packet());
    const response = await f.app.request(`/ui/decisions/${row.id}/withdraw`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ expected_revision: "1", reason: "No longer needed", continue_to: "https://example.com/steal?selected=//evil" }),
    });
    expect(response.headers.get("location")).toBe(`/ui?triage=1&completed=withdrawn&completed_id=${row.id}`);
    expect(f.service.get(row.id)!.state).toBe("cancelled");
    const page = await (await f.app.request(response.headers.get("location")!)).text();
    expect(page).toContain("Request withdrawn.");
    expect(page).toContain('data-transient-notice="triage"');
    expect(page).toContain(`aria-label="View ${packet().question}"`);
  });
  test("reconciling a native reply returns to its exact handled message", async () => {
    const f = fixture();
    const event = f.store.ingestEvent(nativeEvent());
    const incident = seedIncident(f.store.db, f.clock, { opened_by_event: event.event_id, state: "escalated" });
    f.store.db.query("UPDATE events SET incident_id=? WHERE id=?").run(incident, event.event_id);
    const escalation = seedEscalation(f.store.db, f.clock, incident, { question: "Approve native deploy?" });
    const reply = recordAnswer(f.store, { escalationId: escalation, actor: "human:web", payload: { approval: false } });
    f.store.db.query("UPDATE human_replies SET state='uncertain' WHERE id=?").run(reply.id);
    const response = await f.app.request(`/ui/replies/${reply.id}/reconcile`, {
      method: "POST", headers: { ...WEB_AUTH_HEADERS, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ expected_revision: String(reply.revision), outcome: "source_confirmed", note: "Checked source; denied deployment and resumed other work." }),
    });
    expect(response.status).toBe(303);
    const destination = response.headers.get("location")!;
    expect(destination).toBe(`/ui/handled?selected=${encodeURIComponent(`native:${escalation}`)}`);
    const page = await (await f.app.request(destination)).text();
    expect(page).toContain("Your reply");
    expect(page).toContain("Denied");
    expect(page).toContain("Source confirmed unblocked");
    expect(page).not.toContain("This item is no longer in this mailbox.");
    const notice = await (await f.app.request(`/ui?triage=1&completed=answered&completed_kind=native&completed_id=${escalation}`)).text();
    expect(notice).toContain(`/ui/handled?selected=${encodeURIComponent(`native:${escalation}`)}`);
  });

  test("guided pagination preserves context and keeps the sentinel selection highlighted", async () => {
    const f = fixture();
    const rows = [];
    for (let i = 0; i < 52; i++) {
      rows.push(f.service.raise(owner, `paged-${i}`, packet({ question: `Decision number ${i}` })));
      f.clock.advance(1000);
    }
    const page = await (await f.app.request("/ui?page=1")).text();
    expect(page).toContain(`/ui/decisions/${rows[50]!.id}?page=1`);
    const selected = await (await f.app.request(`/ui/decisions/${rows[50]!.id}`)).text();
    expect(selected).toContain(`class="mail-row mail-row-selected" href="/ui/decisions/${rows[50]!.id}"`);
    expect(selected).toContain('aria-label="Selected decision"');
  });

  test("off-page native and delivery selections do not disappear at the lookahead boundary", async () => {
    const f = fixture();
    const nativeIds: string[] = [];
    const replyIds: string[] = [];
    for (let i = 0; i < 52; i++) {
      const event = f.store.ingestEvent(nativeEvent());
      const incident = seedIncident(f.store.db, f.clock, { opened_by_event: event.event_id, state: "escalated" });
      f.store.db.query("UPDATE events SET incident_id=? WHERE id=?").run(incident, event.event_id);
      nativeIds.push(seedEscalation(f.store.db, f.clock, incident, { question: `Native question ${i}` }));
      replyIds.push(recordSessionReply(f.store, { idempotencyKey: `paged-reply-${i}`, actor: "human:web", carSessionId: `session-${i}`, channel: null, payload: { text: `Exact reply ${i}` } }).id);
      f.store.db.query("UPDATE human_replies SET state='uncertain' WHERE id=?").run(replyIds.at(-1)!);
      f.clock.advance(1000);
    }
    for (const index of [0, 1]) {
      const native = await (await f.app.request(`/ui?selected=${encodeURIComponent(`native:${nativeIds[index]}`)}`)).text();
      expect(native).toContain(`class="mail-row mail-row-selected" href="/ui?selected=${encodeURIComponent(`native:${nativeIds[index]}`)}"`);
      expect(native).not.toContain("This item is no longer in this mailbox.");
      const delivery = await (await f.app.request(`/ui/watching?selected=${encodeURIComponent(`delivery:${replyIds[index]}`)}`)).text();
      expect(delivery).toContain(`Exact reply ${index}`);
      expect(delivery).not.toContain("This item is no longer in this mailbox.");
    }
    const wrongTab = await (await f.app.request(`/ui?selected=${encodeURIComponent(`delivery:${replyIds[0]}`)}`)).text();
    expect(wrongTab).toContain("This item is no longer in this mailbox.");
    expect(wrongTab).not.toContain("Exact reply 0");
  });

  test("renders the compact inbox with an auto-selected desktop reader", async () => {
    const f = fixture();
    const row = f.service.raise(owner, "mailbox-guided", packet());

    const response = await f.app.request("/ui");
    const body = await response.text();
    expect(response.status).toBe(200);
    expect(body).toContain('class="mailbox"');
    expect(body).toContain('<section class="mailbox">');
    expect(body).toContain('class="mailbox-list"');
    expect(body).toContain('class="mailbox-reader"');
    expect(body).toContain('class="mail-row mail-row-selected"');
    expect(body).toContain('aria-current="page"');
    expect(body).toContain(`/ui/decisions/${row.id}`);
    expect(body).toContain('data-format="compact"');
    expect(body).toContain("Keep the compatibility bridge?");
  });

  test("guided detail keeps the canonical state tab and explicit mobile selection", async () => {
    const f = fixture();
    const row = f.service.raise(owner, "mailbox-guided-detail", packet());

    const response = await f.app.request(`/ui/decisions/${row.id}`);
    const body = await response.text();
    expect(response.status).toBe(200);
    expect(body).toContain('class="mailbox mailbox-selected"');
    expect(body).toContain('href="#selected-decision"');
    expect(body).toContain('id="selected-decision"');
    expect(body).toContain('aria-label="Selected decision"');
    expect(body).toContain('href="/ui"');
    expect(body).toContain("Needs you");
  });

  test("native rows stay reachable and use source outcome before delivery state", async () => {
    const f = fixture();
    const event = f.store.ingestEvent(nativeEvent());
    const incident = seedIncident(f.store.db, f.clock, { opened_by_event: event.event_id, state: "escalated" });
    f.store.db.query("UPDATE events SET incident_id=? WHERE id=?").run(incident, event.event_id);
    const escalation = seedEscalation(f.store.db, f.clock, incident, { severity: "urgent", question: "Approve native deploy?" });
    const key = `native:${escalation}`;

    const list = await (await f.app.request("/ui")).text();
    expect(list).toContain(`/ui?selected=${encodeURIComponent(key)}`);
    expect(list).toContain('class="mail-row-state urgent">Urgent');

    const selected = await (await f.app.request(`/ui?selected=${encodeURIComponent(key)}`)).text();
    expect(selected).toContain('class="mailbox mailbox-selected"');
    expect(selected).toContain("Approve native deploy?");
    expect(selected).toContain("Native integration");
    expect(selected).not.toContain(">open<");
  });

  test("standalone delivery rows remain selectable and stale selections recover", async () => {
    const f = fixture();
    const reply = recordSessionReply(f.store, {
      idempotencyKey: "mailbox-delivery",
      actor: "human:web",
      carSessionId: "source-session",
      channel: null,
      payload: { text: "Please continue with the migration." },
    });
    f.store.db.query("UPDATE human_replies SET state='uncertain', last_error='Remote acceptance was not confirmed' WHERE id=?").run(reply.id);
    const key = `delivery:${reply.id}`;

    const list = await (await f.app.request("/ui/watching")).text();
    expect(list).toContain(`/ui/watching?selected=${encodeURIComponent(key)}`);
    const selected = await (await f.app.request(`/ui/watching?selected=${encodeURIComponent(key)}`)).text();
    expect(selected).toContain("CAR reply delivery");
    expect(selected).toContain("Please continue with the migration.");
    expect(selected).toContain("Check whether this reply reached the source");

    const stale = await (await f.app.request("/ui/watching?selected=delivery%3Amissing")).text();
    expect(stale).toContain('class="mailbox mailbox-selected"');
    expect(stale).toContain("This item is no longer in this mailbox.");
  });
});
