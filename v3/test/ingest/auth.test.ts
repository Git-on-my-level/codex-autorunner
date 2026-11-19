/**
 * Ingest auth: explicit per-source credentials everywhere, including localhost.
 */
import { describe, expect, test } from "bun:test";
import { harness, LOCALHOST, REMOTE } from "./harness.ts";
import { agentctlCallbackCapability, bearerToken, isLoopback, timingSafeEqual } from "../../src/ingest/auth.ts";

const EVENT = {
  contract: "car.event.v1",
  idempotency_key: "auth-test:1",
  ts: "2026-08-26T12:00:00Z",
  source: { vendor: "cron", host: "mac-studio", adapter: "curl" },
  type: "note",
};

const TOKENS = {
  http: { ingest_tokens: { agentctl: "tok-agentctl", generic: "tok-generic" } },
};

describe("ingest auth", () => {
  test("localhost without a token is rejected", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", EVENT, { from: LOCALHOST, auth: false });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthorized", detail: "missing_bearer_token" });
  });

  test("loopback callers are accepted only with credentials", async () => {
    const h = harness(TOKENS);
    expect((await h.post("/v1/events", EVENT, { from: "::1", auth: false })).status).toBe(401);
    expect(
      (await h.post("/v1/events", { ...EVENT, idempotency_key: "auth-test:2" }, {
        from: "::ffff:127.0.0.1",
        headers: { authorization: "Bearer tok-generic" },
      }))
        .status,
    ).toBe(200);
  });

  test("a remote peer without a token is rejected", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", EVENT, { from: REMOTE, auth: false });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthorized", detail: "missing_bearer_token" });
    expect(res.headers.get("www-authenticate")).toContain("car-ingest");
    expect(h.events()).toHaveLength(0);
  });

  test("a remote peer with the right per-source token is accepted", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", EVENT, {
      from: REMOTE,
      headers: { authorization: "Bearer tok-generic" },
    });
    expect(res.status).toBe(200);
    expect(h.events()).toHaveLength(1);
  });

  test("generic wire input cannot self-attest a verified repository", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", {
      ...EVENT,
      idempotency_key: "auth-test:forged-repo",
      session: {
        vendor: "other",
        native_id: "forged-session",
        host: "mac-studio",
        cwd: "/tmp/claimed-repo",
        repo: "github.com/acme/privileged",
        repo_verified: true,
      },
    }, { headers: { authorization: "Bearer tok-generic" } });
    expect(res.status).toBe(200);
    expect(h.store.db.query("SELECT repo, repo_verified FROM sessions").get()).toEqual({
      repo: "github.com/acme/privileged",
      repo_verified: 0,
    });
  });

  test("a token belonging to a different source does not open this route", async () => {
    const h = harness(TOKENS);
    // tok-agentctl is valid for /v1/ingest/agentctl, never for /v1/events.
    const res = await h.post("/v1/events", EVENT, {
      from: REMOTE,
      headers: { authorization: "Bearer tok-agentctl" },
    });
    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ detail: "invalid_token" });
  });

  test("the wildcard token key opens every source", async () => {
    const h = harness({ http: { ingest_tokens: { "*": "tok-all" } } });
    for (const path of ["/v1/ingest/agentctl", "/v1/ingest/multica"]) {
      const res = await h.post(
        path,
        { nonsense: true },
        { from: REMOTE, headers: { authorization: "Bearer tok-all" } },
      );
      // 400 (not 401) proves auth passed and the normalizer rejected the body.
      expect(res.status).toBe(400);
    }
  });

  test("agentctl continuation callbacks use an execution-scoped URL capability", async () => {
    const h = harness(TOKENS);
    const cap = agentctlCallbackCapability("tok-agentctl", "exec-77");
    const event = { id: "event-77", execution_id: "exec-77", kind: "started", ordering: "observation", occurred_at: EVENT.ts };
    expect((await h.post(`/v1/ingest/agentctl/callback/exec-77/${cap}`, event, { auth: false })).status).toBe(200);
    expect((await h.post("/v1/ingest/agentctl/callback/exec-77/bad", event, { auth: false })).status).toBe(401);
    expect((await h.post(
      `/v1/ingest/agentctl/callback/exec-77/${cap}`,
      { ...event, id: "event-88", execution_id: "exec-88" },
      { auth: false },
    )).status).toBe(400);
  });

  test("a source with no configured token stays shut to remote peers", async () => {
    const h = harness({ http: { ingest_tokens: { generic: "tok-generic" } } });
    const res = await h.post(
      "/v1/ingest/claude",
      { hook_event_name: "Stop", session_id: "s1" },
      { from: REMOTE, headers: { authorization: "Bearer tok-generic" } },
    );
    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ detail: "no_token_configured_for_source" });
  });

  test("an unidentifiable peer fails closed, it is not assumed to be localhost", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", EVENT, { from: null, auth: false });
    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ detail: "unidentified_peer_and_missing_token" });
  });

  test("proxy headers cannot forge localhost", async () => {
    const h = harness(TOKENS);
    const res = await h.post("/v1/events", EVENT, {
      from: REMOTE,
      auth: false,
      headers: { "x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1" },
    });
    expect(res.status).toBe(401);
  });

  test("denials are audited", async () => {
    const h = harness(TOKENS);
    await h.post("/v1/events", EVENT, { from: REMOTE, auth: false });
    const denials = h.audits("ingest.denied");
    expect(denials).toHaveLength(1);
    expect(denials[0]!.object_id).toBe("generic");
  });

  test("GET routes are open (healthz and schema carry no secrets)", async () => {
    const h = harness(TOKENS);
    expect((await h.get("/healthz", { from: REMOTE })).status).toBe(200);
    expect((await h.get("/v1/schema", { from: REMOTE })).status).toBe(200);
  });
});

describe("auth primitives", () => {
  test("bearerToken parses case-insensitively and rejects empties", () => {
    expect(bearerToken("Bearer abc")).toBe("abc");
    expect(bearerToken("bearer  abc  ")).toBe("abc");
    expect(bearerToken("Basic abc")).toBeNull();
    expect(bearerToken("Bearer ")).toBeNull();
    expect(bearerToken(undefined)).toBeNull();
  });

  test("isLoopback only accepts real loopback literals", () => {
    expect(isLoopback("127.0.0.1")).toBe(true);
    expect(isLoopback("::1")).toBe(true);
    expect(isLoopback(null)).toBe(false);
    expect(isLoopback("127.0.0.2")).toBe(false);
    expect(isLoopback("10.0.0.1")).toBe(false);
  });

  test("timingSafeEqual compares content, not identity", () => {
    expect(timingSafeEqual("abc", "abc")).toBe(true);
    expect(timingSafeEqual("abc", "abd")).toBe(false);
    expect(timingSafeEqual("abc", "abcd")).toBe(false);
  });
});
