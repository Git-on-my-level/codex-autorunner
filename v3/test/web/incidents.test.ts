import { describe, expect, test } from "bun:test";
import { FakeClock, memoryStore } from "../fakes.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { buildDeps, mountApp, seedIncident, seedDecision, seedAction, seedEscalation, seedOutcome } from "./helpers.ts";

describe("web incidents", () => {
  test("GET /ui/incidents lists open/escalated incidents and links to detail", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    const openId = seedIncident(deps.store.db, clock, { state: "open", summary: "Needs a look" });
    seedIncident(deps.store.db, clock, { state: "resolved", summary: "Already handled" });
    const app = mountApp(deps);

    const res = await app.request("/ui/incidents");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("Needs a look");
    expect(body).not.toContain("Already handled");
    expect(body).toContain(`/ui/incidents/${openId}`);
  });

  test("state filter can select resolved incidents explicitly", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    seedIncident(deps.store.db, clock, { state: "resolved", summary: "Already handled" });
    const app = mountApp(deps);

    const res = await app.request("/ui/incidents?state=resolved");
    const body = await res.text();
    expect(body).toContain("Already handled");
  });

  test("GET /ui/incidents/:id renders the full why-did-CAR-do-that chain", async () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const deps = buildDeps({ store });

    const ingested = store.ingestEvent(
      parseEvent({
        contract: CONTRACT_VERSION,
        idempotency_key: "chain-1",
        ts: clock.now().toISOString(),
        source: { vendor: "codex", host: "mac", adapter: "subscribe-webhook" },
        session: { vendor: "codex", native_id: "sess-chain", host: "mac", repo: "github.com/x/y" },
        type: "attention.question",
        severity: "attention",
        requires_response: true,
        title: "Force-push to fix/telemetry-cliff?",
        body: "remote diverged, 2 CI commits ahead",
      }),
    );

    const incidentId = seedIncident(store.db, clock, {
      car_session_id: ingested.car_session_id,
      opened_by_event: ingested.event_id,
      state: "escalated",
      summary: "Force-push question",
      dedupe_class: "force-push",
    });
    store.db.query("UPDATE events SET incident_id = ? WHERE id = ?").run(incidentId, ingested.event_id);

    const decisionId = seedDecision(store.db, clock, incidentId, {
      decided_by: "llm",
      disposition: "escalate",
      rationale: "David denied force-push on this repo twice before.",
      model: "anthropic/claude-haiku-4-5",
      tokens_in: 500,
      tokens_out: 120,
      cost_usd: 0.0042,
    });
    seedAction(store.db, decisionId, { class: "probe.git_status", policy_verdict: "auto", state: "ok" });

    const escalationId = seedEscalation(store.db, clock, incidentId, {
      severity: "attention",
      question: "Force-push to fix/telemetry-cliff? Remote diverged.",
      suggested_action_json: JSON.stringify({ label: "DENY — tell agent to rebase instead" }),
      state: "answered",
      answered_by: "david",
      answer_json: JSON.stringify({ approval: false }),
      answered_at: clock.now().toISOString(),
    });

    seedOutcome(store.db, clock, {
      decision_id: decisionId,
      escalation_id: escalationId,
      verdict: "confirmed",
      note: "David agreed with the deny suggestion.",
    });

    store.audit("llm", "decision.made", "decision", decisionId, { disposition: "escalate" });

    const app = mountApp(deps);
    const res = await app.request(`/ui/incidents/${incidentId}`);
    expect(res.status).toBe(200);
    const body = await res.text();

    // events
    expect(body).toContain("Force-push to fix/telemetry-cliff?");
    // decisions: rationale, model, cost
    expect(body).toContain("David denied force-push on this repo twice before.");
    expect(body).toContain("anthropic/claude-haiku-4-5");
    expect(body).toContain("0.0042");
    // escalations: question, suggested action, answer
    expect(body).toContain("Force-push to fix/telemetry-cliff? Remote diverged.");
    expect(body).toContain("DENY");
    // outcomes
    expect(body).toContain("David agreed with the deny suggestion.");
    expect(body).toContain("confirmed");
    // audit
    expect(body).toContain("decision.made");
  });

  test("GET /ui/incidents/:id 404s for an unknown incident", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const res = await app.request("/ui/incidents/does-not-exist");
    expect(res.status).toBe(404);
  });
});
