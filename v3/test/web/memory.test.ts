import { describe, expect, test } from "bun:test";
import { FakeClock, memoryStore } from "../fakes.ts";
import { buildDeps, mountApp, seedMemory } from "./helpers.ts";

describe("web memory", () => {
  test("GET /ui/memory renders rules sorted by confidence with badges and pending/notes sections", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    seedMemory(deps.store.db, clock, {
      tier: "rule",
      autonomy: "granted",
      confidence: 0.9,
      content: { match: { type: "attention.permission" }, disposition: "auto_resolve", action_class: "reply" },
    });
    seedMemory(deps.store.db, clock, { tier: "rule", autonomy: "suggest", confidence: 0.4 });
    seedMemory(deps.store.db, clock, { tier: "note", authored_by: "david", content: { text: "prefers rebase" } });
    seedMemory(deps.store.db, clock, { status: "pending", authored_by: "triage", content: { text: "proposed rule" } });
    const app = mountApp(deps);

    const res = await app.request("/ui/memory");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("granted");
    expect(body).toContain("suggest");
    expect(body).toContain("prefers rebase");
    expect(body).toContain("proposed rule");
    expect(body).toContain("approve");
    expect(body).toContain("reject");
    // v1 web never offers promote-to-granted
    expect(body).not.toContain('value="promote"');
  });

  /*
   * A rule minted by the Telegram "always" tap stores the original question
   * alongside a dedupe-class match key. Rendering only the match key would show
   * a hash on the one page where David reviews what he has granted.
   */
  test("a granted rule shows the question it came from, not just its match key", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    seedMemory(deps.store.db, clock, {
      tier: "rule",
      autonomy: "granted",
      content: {
        match: "claude-code:sess-abc:PermissionRequest:a24c750eeb79",
        disposition: "auto_resolve",
        action_class: "approve_permission",
        question: "Permission: Bash: bun test",
      },
    });

    const body = await (await mountApp(deps).request("/ui/memory")).text();
    expect(body).toContain("Permission: Bash: bun test");
    expect(body).toContain("approve_permission");
  });

  test("archive POST sets status=archived and writes an audit row", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    const id = seedMemory(deps.store.db, clock, { tier: "rule", status: "active", autonomy: "suggest" });
    const app = mountApp(deps);

    const res = await app.request(`/ui/memory/${id}/archive`, { method: "POST" });
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/ui/memory");

    const row = deps.store.db.query("SELECT status FROM memories WHERE id = ?").get(id) as { status: string };
    expect(row.status).toBe("archived");

    const audit = deps.store.db
      .query("SELECT actor, verb FROM audit WHERE object_type = 'memory' AND object_id = ? AND verb = 'memory.archived'")
      .get(id) as { actor: string; verb: string } | null;
    expect(audit).not.toBeNull();
    expect(audit!.actor).toBe("david");
  });

  test("demote POST steps autonomy granted -> suggest -> none and writes audit each time", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    const id = seedMemory(deps.store.db, clock, { tier: "rule", autonomy: "granted" });
    const app = mountApp(deps);

    let res = await app.request(`/ui/memory/${id}/demote`, { method: "POST" });
    expect(res.status).toBe(303);
    let row = deps.store.db.query("SELECT autonomy FROM memories WHERE id = ?").get(id) as { autonomy: string };
    expect(row.autonomy).toBe("suggest");

    res = await app.request(`/ui/memory/${id}/demote`, { method: "POST" });
    expect(res.status).toBe(303);
    row = deps.store.db.query("SELECT autonomy FROM memories WHERE id = ?").get(id) as { autonomy: string };
    expect(row.autonomy).toBe("none");

    const auditCount = deps.store.db
      .query("SELECT COUNT(*) n FROM audit WHERE object_type = 'memory' AND object_id = ? AND verb = 'memory.autonomy_set'")
      .get(id) as { n: number };
    expect(auditCount.n).toBe(2);
  });

  test("archive/demote 404 for an unknown memory id", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    expect((await app.request("/ui/memory/nope/archive", { method: "POST" })).status).toBe(404);
    expect((await app.request("/ui/memory/nope/demote", { method: "POST" })).status).toBe(404);
  });

  test("proposal approve sets status=active, reject sets status=archived, both audited as david", async () => {
    const clock = new FakeClock();
    const deps = buildDeps({ store: memoryStore(clock) });
    const approveId = seedMemory(deps.store.db, clock, { status: "pending", authored_by: "triage" });
    const rejectId = seedMemory(deps.store.db, clock, { status: "pending", authored_by: "triage" });
    const app = mountApp(deps);

    const approveRes = await app.request(`/ui/memory/${approveId}/approve`, { method: "POST" });
    expect(approveRes.status).toBe(303);
    const approved = deps.store.db.query("SELECT status FROM memories WHERE id = ?").get(approveId) as {
      status: string;
    };
    expect(approved.status).toBe("active");

    const rejectRes = await app.request(`/ui/memory/${rejectId}/reject`, { method: "POST" });
    expect(rejectRes.status).toBe(303);
    const rejected = deps.store.db.query("SELECT status FROM memories WHERE id = ?").get(rejectId) as {
      status: string;
    };
    expect(rejected.status).toBe("archived");

    const approveAudit = deps.store.db
      .query("SELECT actor FROM audit WHERE object_id = ? AND verb = 'memory.proposal_approved'")
      .get(approveId) as { actor: string } | null;
    const rejectAudit = deps.store.db
      .query("SELECT actor FROM audit WHERE object_id = ? AND verb = 'memory.proposal_rejected'")
      .get(rejectId) as { actor: string } | null;
    expect(approveAudit?.actor).toBe("david");
    expect(rejectAudit?.actor).toBe("david");
  });

  test("adding a note writes a memory row (authored_by david) and an audit row, then redirects", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);

    const res = await app.request("/ui/memory/notes", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "David prefers rebase over merge", repo: "github.com/x/y" }).toString(),
    });
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/ui/memory?added=1");

    const row = deps.store.db
      .query("SELECT tier, kind, content_json, scope_json, authored_by, status FROM memories WHERE tier = 'note'")
      .get() as { tier: string; kind: string; content_json: string; scope_json: string; authored_by: string; status: string };
    expect(row.authored_by).toBe("david");
    expect(row.status).toBe("active");
    expect(JSON.parse(row.content_json).text).toBe("David prefers rebase over merge");
    expect(JSON.parse(row.scope_json).repo).toBe("github.com/x/y");

    const audit = deps.store.db
      .query("SELECT verb FROM audit WHERE object_type = 'memory' AND verb = 'memory.created'")
      .get() as { verb: string } | null;
    expect(audit).not.toBeNull();
  });

  test("blank note text is rejected without writing a row", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const before = (deps.store.db.query("SELECT COUNT(*) n FROM memories").get() as { n: number }).n;

    const res = await app.request("/ui/memory/notes", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "   " }).toString(),
    });
    expect(res.status).toBe(303);
    const after = (deps.store.db.query("SELECT COUNT(*) n FROM memories").get() as { n: number }).n;
    expect(after).toBe(before);
  });
});
