import { describe, expect, test } from "bun:test";
import { buildDeps, mountApp } from "./helpers.ts";

describe("web digests", () => {
  test("renders archived digests newest first as <pre> blocks", async () => {
    const deps = buildDeps();
    deps.store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)")
      .run("2026-08-24", "# CAR digest — Mon Aug 24\nquiet day", "2026-08-24T08:30:00Z");
    deps.store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)")
      .run("2026-08-25", "# CAR digest — Tue Aug 25\nbusy day", null);
    const app = mountApp(deps);

    const res = await app.request("/ui/digests");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("<pre>");
    expect(body).toContain("quiet day");
    expect(body).toContain("busy day");
    expect(body.indexOf("2026-08-25")).toBeLessThan(body.indexOf("2026-08-24"));
  });

  test("shows an empty state with no digests", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const res = await app.request("/ui/digests");
    const body = await res.text();
    expect(body).toContain("No digests recorded yet.");
  });
});
