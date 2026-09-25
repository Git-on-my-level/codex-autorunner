import { expect, test } from "bun:test";
import { buildDeps, mountApp } from "./helpers.ts";

test("HTML responses use standards mode without losing response security headers", async () => {
  const deps = buildDeps();
  try {
    const app = mountApp(deps);
    for (const path of ["/ui", "/ui/login", "/ui/settings", "/ui/events"]) {
      const response = await app.request(path);
      expect(response.status).toBe(200);
      expect((await response.text()).startsWith("<!doctype html>")).toBe(true);
      expect(response.headers.get("content-security-policy")).toContain("form-action 'self'");
      expect(response.headers.get("cache-control")).toBe("no-store");
    }
    const denied = await app.request("/ui/logout", { method: "POST" });
    expect(denied.status).toBe(401);
    expect((await denied.text()).startsWith("<!doctype html>")).toBe(true);
    const script = await app.request("/ui/live-refresh.js");
    expect((await script.text()).startsWith("<!doctype")).toBe(false);
  } finally { deps.store.db.close(); }
});
