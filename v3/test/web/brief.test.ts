import { describe, expect, test } from "bun:test";
import { FakeClock, memoryStore } from "../fakes.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { buildDeps, mountApp, seedIncident, seedEscalation } from "./helpers.ts";

describe("web brief.md", () => {
  test("lists open escalations with age and includes yesterday's digest", async () => {
    const clock = new FakeClock(new Date("2026-08-26T18:00:00Z"));
    const store = memoryStore(clock);
    const deps = buildDeps({ store });

    // an escalation opened 5 hours ago
    clock.advance(-5 * 3_600_000);
    const incidentId = seedIncident(store.db, clock, { state: "escalated" });
    const escId = seedEscalation(store.db, clock, incidentId, {
      question: "Force-push to fix/telemetry-cliff?",
      state: "pending",
    });
    clock.advance(5 * 3_600_000); // back to "now"

    store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)")
      .run("2026-08-25", "# CAR digest — Mon Aug 25\nHandled 3 things.", "2026-08-25T08:30:00Z");

    const app = mountApp(deps);
    const res = await app.request("/ui/brief.md");
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/markdown");
    const body = await res.text();

    expect(body).toContain(escId);
    expect(body).toContain("Force-push to fix/telemetry-cliff?");
    expect(body).toContain("5.0h");
    expect(body).toContain("Handled 3 things.");
  });

  test("reports stuck sessions with an outstanding requires_response event", async () => {
    const clock = new FakeClock(new Date("2026-08-26T18:00:00Z"));
    const store = memoryStore(clock);
    const deps = buildDeps({ store });

    clock.advance(-3 * 3_600_000);
    store.ingestEvent(
      parseEvent({
        contract: CONTRACT_VERSION,
        idempotency_key: "stuck-1",
        ts: clock.now().toISOString(),
        source: { vendor: "codex", host: "mac", adapter: "subscribe-webhook" },
        session: { vendor: "codex", native_id: "sess-stuck", host: "mac", title: "omi-desktop" },
        type: "attention.idle",
        severity: "attention",
        requires_response: true,
        title: "waiting on input",
      }),
    );
    clock.advance(3 * 3_600_000);

    const app = mountApp(deps);
    const res = await app.request("/ui/brief.md");
    const body = await res.text();
    expect(body).toContain("omi-desktop");
    expect(body).toContain("3.0h");
  });

  test("says so plainly when there is nothing open", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const res = await app.request("/ui/brief.md");
    const body = await res.text();
    expect(body).toContain("Open escalations (0)");
    expect(body).toContain("_none_");
    expect(body).toContain("no digest recorded for yesterday");
  });
});
