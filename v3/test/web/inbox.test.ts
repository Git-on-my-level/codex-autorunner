import { describe, expect, test } from "bun:test";
import { FakeClock, memoryStore } from "../fakes.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { buildDeps, mountApp } from "./helpers.ts";

function ev(overrides: Record<string, unknown> = {}) {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: `k-${Math.random()}`,
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "claude-code", host: "mac", adapter: "hook-http" },
    session: { vendor: "claude-code", native_id: "s1", host: "mac", repo: "github.com/x/y" },
    type: "attention.question",
    severity: "attention",
    requires_response: true,
    title: "Which migration strategy?",
    body: "picking between squash and rebase",
    ...overrides,
  });
}

describe("web inbox", () => {
  test("GET /ui renders seeded events with core columns", async () => {
    const deps = buildDeps();
    deps.store.ingestEvent(ev({ idempotency_key: "k1", title: "Approve deploy?" }));
    deps.store.ingestEvent(
      ev({ idempotency_key: "k2", type: "attention.error", severity: "urgent", title: "Build failed" }),
    );
    const app = mountApp(deps);

    const res = await app.request("/ui");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("Approve deploy?");
    expect(body).toContain("Build failed");
    expect(body).toContain("claude-code");
    expect(body).toContain("attention.question");
    expect(body).toContain("urgent");
  });

  test("filters by vendor", async () => {
    const deps = buildDeps();
    deps.store.ingestEvent(
      ev({ idempotency_key: "kv1", title: "Codex thing", source: { vendor: "codex", host: "mac", adapter: "x" } }),
    );
    deps.store.ingestEvent(
      ev({ idempotency_key: "kv2", title: "Claude thing", source: { vendor: "claude-code", host: "mac", adapter: "x" } }),
    );
    const app = mountApp(deps);

    const res = await app.request("/ui?vendor=codex");
    const body = await res.text();
    expect(body).toContain("Codex thing");
    expect(body).not.toContain("Claude thing");
  });

  test("filters by severity", async () => {
    const deps = buildDeps();
    deps.store.ingestEvent(ev({ idempotency_key: "ks1", severity: "urgent", title: "Urgent one" }));
    deps.store.ingestEvent(ev({ idempotency_key: "ks2", severity: "info", title: "Info one" }));
    const app = mountApp(deps);

    const res = await app.request("/ui?severity=urgent");
    const body = await res.text();
    expect(body).toContain("Urgent one");
    expect(body).not.toContain("Info one");
  });

  test("filters by triage state", async () => {
    const deps = buildDeps();
    const a = deps.store.ingestEvent(ev({ idempotency_key: "kt1", title: "Resolved one" }));
    deps.store.ingestEvent(ev({ idempotency_key: "kt2", title: "Pending one" }));
    deps.store.setEventTriageState(a.event_id, "rules_resolved");
    const app = mountApp(deps);

    const res = await app.request("/ui?state=rules_resolved");
    const body = await res.text();
    expect(body).toContain("Resolved one");
    expect(body).not.toContain("Pending one");
  });

  test("filters by repo", async () => {
    const deps = buildDeps();
    deps.store.ingestEvent(
      ev({
        idempotency_key: "kr1",
        title: "Repo A event",
        session: { vendor: "claude-code", native_id: "sa", host: "mac", repo: "github.com/a/a" },
      }),
    );
    deps.store.ingestEvent(
      ev({
        idempotency_key: "kr2",
        title: "Repo B event",
        session: { vendor: "claude-code", native_id: "sb", host: "mac", repo: "github.com/b/b" },
      }),
    );
    const app = mountApp(deps);

    const res = await app.request("/ui?repo=github.com%2Fa%2Fa");
    const body = await res.text();
    expect(body).toContain("Repo A event");
    expect(body).not.toContain("Repo B event");
  });

  test("free-text search matches title and body", async () => {
    const deps = buildDeps();
    deps.store.ingestEvent(ev({ idempotency_key: "kq1", title: "Force push warning", body: "" }));
    deps.store.ingestEvent(ev({ idempotency_key: "kq2", title: "Something else", body: "mentions force-push here" }));
    deps.store.ingestEvent(ev({ idempotency_key: "kq3", title: "Unrelated", body: "nothing to see" }));
    const app = mountApp(deps);

    const res = await app.request("/ui?q=force");
    const body = await res.text();
    expect(body).toContain("Force push warning");
    expect(body).toContain("Something else");
    expect(body).not.toContain("Unrelated");
  });

  test("before cursor excludes events at/after the cursor", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    deps.store.ingestEvent(ev({ idempotency_key: "kb1", title: "Earlier event" }));
    clock.advance(60_000);
    deps.store.ingestEvent(ev({ idempotency_key: "kb2", title: "Later event" }));
    const rows = deps.store.db.query("SELECT id, received_at FROM events ORDER BY received_at ASC").all() as {
      id: string;
      received_at: string;
    }[];
    expect(rows.length).toBe(2);
    const app = mountApp(deps);

    const res = await app.request(`/ui?before=${encodeURIComponent(rows[1]!.received_at)}`);
    const body = await res.text();
    expect(body).toContain("Earlier event");
    expect(body).not.toContain("Later event");
  });

  test("incident_id column links to the incident detail page when attached", async () => {
    const deps = buildDeps();
    const inserted = deps.store.ingestEvent(ev({ idempotency_key: "kinc", title: "Linked event" }));
    deps.store.db
      .query("UPDATE events SET incident_id = 'inc_test1' WHERE id = ?")
      .run(inserted.event_id);
    const app = mountApp(deps);

    const res = await app.request("/ui");
    const body = await res.text();
    expect(body).toContain("/ui/incidents/inc_test1");
  });
});
