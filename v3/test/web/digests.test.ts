import { describe, expect, test } from "bun:test";
import { buildDeps, mountApp } from "./helpers.ts";

describe("web digests", () => {
  test("renders readable digest cards newest first and preserves raw markdown", async () => {
    const deps = buildDeps();
    deps.store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)")
      .run("2026-08-24", "# CAR digest — Mon Aug 24\nquiet day", "2026-08-24T08:30:00Z");
    deps.store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)")
      .run("2026-08-25", "# CAR digest — Tue Aug 25\nbusy day", null);
    const delivered = deps.store.enqueueOutbox("telegram", { chat_id: "test" }, { kind: "digest" });
    deps.store.attachDigestOutbox("2026-08-24", delivered);
    deps.store.db.query("UPDATE outbox SET state = 'delivered', sent_message_id = 'msg-24' WHERE id = ?").run(delivered);
    const uncertain = deps.store.enqueueOutbox("telegram", { chat_id: "test" }, { kind: "digest" });
    deps.store.attachDigestOutbox("2026-08-25", uncertain);
    deps.store.db.query("UPDATE outbox SET state = 'uncertain' WHERE id = ?").run(uncertain);
    const app = mountApp(deps);

    const res = await app.request("/ui/digests");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("Delivery &amp; raw data");
    expect(body).toContain("Delivered");
    expect(body).toContain("Delivery uncertain");
    expect(body).toContain("Verify remotely before retrying");
    expect(body).toContain("msg-24");
    expect(body).toContain("quiet day");
    expect(body).toContain("busy day");
    expect(body.indexOf("2026-08-25")).toBeLessThan(body.indexOf("2026-08-24"));
  });

  test("shows an empty state with no digests", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const res = await app.request("/ui/digests");
    const body = await res.text();
    expect(body).toContain("No digests yet");
    expect(body).toContain("Completed summaries will appear here");
  });
});
