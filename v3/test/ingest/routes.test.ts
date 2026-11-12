/**
 * End-to-end route behaviour: each source normalizer wired to the store through
 * its HTTP endpoint, plus /healthz, /v1/schema and extraApps mounting.
 */
import { describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { fixtureInput, harness } from "./harness.ts";
import { eventJsonSchema, SCHEMA_ID, resetSchemaCache } from "../../src/ingest/schema.ts";

describe("GET /healthz", () => {
  test("reports the contract it speaks", async () => {
    const h = harness();
    expect(await (await h.get("/healthz")).json()).toEqual({ ok: true, contract: "car.event.v1" });
  });
});

describe("GET /v1/schema", () => {
  test("serves a JSON Schema derived from the frozen zod contract", async () => {
    resetSchemaCache();
    const h = harness();
    const res = await h.get("/v1/schema");
    expect(res.status).toBe(200);

    const schema = (await res.json()) as Record<string, any>;
    expect(schema["$schema"]).toBe("https://json-schema.org/draft/2020-12/schema");
    expect(schema["$id"]).toBe(SCHEMA_ID);
    expect(schema["title"]).toBe("car.event.v1");
    expect(schema["type"]).toBe("object");
  });

  test("the closed vocabularies are present and complete", () => {
    resetSchemaCache();
    const schema = eventJsonSchema() as any;
    const props = schema.properties;

    expect(props.contract.const).toBe("car.event.v1");
    expect(props.type.enum).toEqual([
      "session.started",
      "session.ended",
      "attention.permission",
      "attention.question",
      "attention.idle",
      "attention.error",
      "attention.cleared",
      "progress",
      "artifact",
      "heartbeat",
      "cost.report",
      "note",
    ]);
    expect(props.severity.enum).toEqual(["info", "notice", "attention", "urgent"]);
    expect(props.source.properties.vendor.enum).toContain("agentctl");
    expect(props.ts.format).toBe("date-time");
  });

  test("only the fields a producer must send are required", () => {
    resetSchemaCache();
    const schema = eventJsonSchema() as any;
    // io:"input" — anything with a contract default stays optional on the wire.
    expect(schema.required.sort()).toEqual(["contract", "idempotency_key", "source", "ts", "type"]);
  });

  test("is memoized but resettable", () => {
    resetSchemaCache();
    expect(eventJsonSchema()).toBe(eventJsonSchema());
  });
});

describe("POST /v1/ingest/agentctl", () => {
  test("a bare journal event lands as session.started", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/agentctl", fixtureInput("agentctl", "01-"));
    expect(res.status).toBe(200);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ accepted: 1, rejected: 0 });

    const rows = h.events();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      type: "session.started",
      idempotency_key: "agentctl:event-cabbage-push-visual-common-foot-endorse",
    });
  });

  test("a callback envelope is unwrapped", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/agentctl", fixtureInput("agentctl", "05-"));
    expect(res.status).toBe(200);
    expect(h.events()[0]).toMatchObject({ type: "attention.permission", severity: "attention" });
  });

  test("at-least-once redelivery is idempotent", async () => {
    const h = harness();
    const payload = fixtureInput("agentctl", "03-");
    await h.post("/v1/ingest/agentctl", payload);
    const second = await h.post("/v1/ingest/agentctl", payload);
    const body = (await second.json()) as { results: { inserted: boolean }[] };
    expect(body.results[0]!.inserted).toBe(false);
    expect(h.events()).toHaveLength(1);
  });

  test("an NDJSON stream of journal events is accepted in one POST", async () => {
    const h = harness();
    const raw = ["01-", "03-"].map((p) => JSON.stringify(fixtureInput("agentctl", p))).join("\n");
    const res = await h.post("/v1/ingest/agentctl", raw, {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ accepted: 2 });
    expect(h.events()).toHaveLength(2);
  });

  test("all events of one execution resolve to a single CAR session", async () => {
    const h = harness();
    for (const prefix of ["01-", "02-", "03-", "07-"]) {
      await h.post("/v1/ingest/agentctl", fixtureInput("agentctl", prefix));
    }
    const sessions = h.store.db.query("SELECT car_session_id FROM sessions").all();
    expect(sessions).toHaveLength(1);
    expect(h.events()).toHaveLength(4);
  });

  test("a payload with no recognizable event is a 400 and writes nothing", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/agentctl", { hello: "world" });
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "invalid_agentctl_event" });
    expect(h.events()).toHaveLength(0);
  });

  test("a batch keeps the good events and reports the bad ones", async () => {
    const h = harness();
    const raw = [JSON.stringify(fixtureInput("agentctl", "01-")), JSON.stringify({ nope: 1 })].join("\n");
    const res = await h.post("/v1/ingest/agentctl", raw, {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect(res.status).toBe(200);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ accepted: 1, rejected: 1 });
  });
});

describe("POST /v1/ingest/claude", () => {
  test("SessionStart is stored and ACKed after commit", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/claude", fixtureInput("claude", "01-"));
    expect(res.status).toBe(200);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ ok: true, inserted: true });
    expect(h.events()[0]).toMatchObject({ type: "session.started" });
  });

  test("a hook payload with no hook_event_name is a 400", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/claude", { session_id: "s1" });
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "invalid_claude_hook" });
  });

  test("malformed JSON is a 400", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/claude", "{nope");
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "invalid_json" });
  });

  test("an unknown hook event is kept as a note rather than dropped", async () => {
    const h = harness();
    await h.post("/v1/ingest/claude", {
      session_id: "s9",
      hook_event_name: "SomeFutureHook",
      cwd: "/tmp",
    });
    expect(h.events()[0]).toMatchObject({ type: "note" });
  });

  test("hooks from one Claude session share one CAR session", async () => {
    const h = harness();
    for (const prefix of ["01-", "03-", "07-", "02-"]) {
      await h.post("/v1/ingest/claude", fixtureInput("claude", prefix));
    }
    expect(h.store.db.query("SELECT car_session_id FROM sessions").all()).toHaveLength(1);
    expect(h.events()).toHaveLength(4);
  });
});

describe("POST /v1/ingest/multica", () => {
  test("an issue question becomes an attention.question with a multica-api channel", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/multica", fixtureInput("multica", "01-"));
    expect(res.status).toBe(200);

    const row = h.store.db
      .query("SELECT type, severity, requires_response, response_channel_json FROM events")
      .get() as { type: string; requires_response: number; response_channel_json: string };
    expect(row.type).toBe("attention.question");
    expect(row.requires_response).toBe(1);
    expect(JSON.parse(row.response_channel_json)).toMatchObject({
      kind: "multica-api",
      hint: { issue: "MUL-128" },
    });
  });

  test("a payload with no issue reference is a 400", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/multica", { action: "updated" });
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "invalid_multica_event" });
  });

  test("delivery ids make redelivery idempotent", async () => {
    const h = harness();
    const payload = fixtureInput("multica", "01-");
    await h.post("/v1/ingest/multica", payload);
    await h.post("/v1/ingest/multica", payload);
    expect(h.events()).toHaveLength(1);
  });
});

describe("composition", () => {
  test("extraApps are mounted under their path", async () => {
    const extra = new Hono();
    extra.get("/brief.md", (c) => c.text("# brief"));
    const h = harness({}, {}, [{ path: "/", app: extra }]);
    const res = await h.get("/brief.md");
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("# brief");
  });

  test("every ingest route ACKs only after the row is committed", async () => {
    const h = harness();
    await h.post("/v1/ingest/agentctl", fixtureInput("agentctl", "01-"));
    await h.post("/v1/ingest/multica", fixtureInput("multica", "01-"));
    await h.post("/v1/ingest/claude", fixtureInput("claude", "01-"));
    await h.post("/v1/events", {
      contract: "car.event.v1",
      idempotency_key: "generic:1",
      ts: "2026-08-26T12:00:00Z",
      source: { vendor: "cron", host: "m", adapter: "curl" },
      type: "note",
    });
    expect(h.events()).toHaveLength(4);
    // Ingest audits one row per committed event.
    expect(h.audits("event.ingested")).toHaveLength(4);
  });
});
