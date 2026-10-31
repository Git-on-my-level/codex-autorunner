import { describe, expect, test } from "bun:test";
import { testConfig } from "../fakes.ts";
import { buildDeps, mountApp, WEB_AUTH_HEADERS, WEB_TEST_TOKEN } from "./helpers.ts";

describe("web write authentication", () => {
  test("opens the UI without a token in optional trusted mode and keeps same-origin writes", async () => {
    const deps = buildDeps({ config: testConfig({ http: { private_reads: true, web_auth: "optional", ingest_tokens: {} } }) });
    const app = mountApp(deps);

    expect((await app.request("http://127.0.0.1/ui")).status).toBe(200);
    const login = await app.request("http://127.0.0.1/ui/login");
    expect(login.status).toBe(303);
    expect(login.headers.get("location")).toBe("/ui");

    const accepted = await app.request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: { origin: "http://127.0.0.1", "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "trusted local note" }).toString(),
    });
    expect(accepted.status).toBe(303);

    const rejected = await app.request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: { origin: "https://evil.example", "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "cross-origin note" }).toString(),
    });
    expect(rejected.status).toBe(401);
    expect((deps.store.db.query("SELECT COUNT(*) AS n FROM memories").get() as { n: number }).n).toBe(1);
  });

  test("can require a token even when no web token is configured", async () => {
    const deps = buildDeps({ config: testConfig({ http: { private_reads: true, web_auth: "required", ingest_tokens: {} } }) });
    const response = await mountApp(deps).request("http://127.0.0.1/ui");
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/ui/login");
  });

  test("does not silently open when a configured web credential is missing", async () => {
    const deps = buildDeps({ config: testConfig({ http: { private_reads: true, web_auth: "optional", ingest_token_envs: { web: "CAR_MISSING_WEB_TOKEN" } } }) });
    const response = await mountApp(deps).request("http://127.0.0.1/ui");
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/ui/login");
  });

  test("rejects an unauthenticated localhost write and audits the denial", async () => {
    const deps = buildDeps();
    const response = await mountApp(deps).request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ text: "must not be written" }).toString(),
    });

    expect(response.status).toBe(401);
    expect((deps.store.db.query("SELECT COUNT(*) AS n FROM memories").get() as { n: number }).n).toBe(0);
    expect(
      deps.store.db
        .query("SELECT verb FROM audit WHERE verb = 'web.write_denied'")
        .get() as { verb: string } | null,
    ).not.toBeNull();
  });

  test("accepts a configured bearer token", async () => {
    const deps = buildDeps();
    const response = await mountApp(deps).request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: {
        ...WEB_AUTH_HEADERS,
        "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ text: "authenticated note" }).toString(),
    });

    expect(response.status).toBe(303);
    expect((deps.store.db.query("SELECT COUNT(*) AS n FROM memories").get() as { n: number }).n).toBe(1);
  });

  test("issues an HttpOnly session and requires same-origin for cookie writes", async () => {
    const deps = buildDeps();
    const app = mountApp(deps);
    const login = await app.request("http://127.0.0.1/ui/login", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: WEB_TEST_TOKEN }).toString(),
    });

    expect(login.status).toBe(303);
    const setCookie = login.headers.get("set-cookie");
    expect(setCookie).toContain("car_ui_session=");
    expect(setCookie?.toLowerCase()).toContain("httponly");
    expect(setCookie?.toLowerCase()).toContain("samesite=strict");
    const cookie = setCookie!.split(";", 1)[0]!;

    const accepted = await app.request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: {
        cookie,
        host: "127.0.0.1",
        origin: "http://127.0.0.1",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ text: "same-origin note" }).toString(),
    });
    expect(accepted.status).toBe(303);

    const rejected = await app.request("http://127.0.0.1/ui/memory/notes", {
      method: "POST",
      headers: {
        cookie,
        host: "127.0.0.1",
        origin: "https://evil.example",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ text: "cross-origin note" }).toString(),
    });
    expect(rejected.status).toBe(401);
    expect((deps.store.db.query("SELECT COUNT(*) AS n FROM memories").get() as { n: number }).n).toBe(1);
  });
});
