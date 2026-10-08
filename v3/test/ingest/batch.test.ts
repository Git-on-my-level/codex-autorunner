/**
 * POST /v1/events body handling: single object, JSON array, NDJSON batch, and
 * the failure modes a naive splitter gets wrong.
 */
import { describe, expect, test } from "bun:test";
import { harness } from "./harness.ts";
import { BatchLineError, BodyError, decodeBody, MAX_BATCH_ITEMS } from "../../src/ingest/batch.ts";

function event(key: string, extra: Record<string, unknown> = {}) {
  return {
    contract: "car.event.v1",
    idempotency_key: key,
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "cron", host: "mac-studio", adapter: "curl" },
    type: "note",
    ...extra,
  };
}

const ndjson = (...objs: unknown[]) => objs.map((o) => JSON.stringify(o)).join("\n");

describe("decodeBody", () => {
  test("a compact single object is not a batch", () => {
    const decoded = decodeBody(JSON.stringify(event("a")), "application/json");
    expect(decoded.mode).toBe("single");
    expect(decoded.items).toHaveLength(1);
  });

  test("a PRETTY-PRINTED single object is not a batch either", () => {
    // The scaffold's `startsWith("{") && includes("\n")` sniff split this into
    // invalid fragments. Whole-body JSON is tried first precisely to fix it.
    const decoded = decodeBody(JSON.stringify(event("a"), null, 2), "application/json");
    expect(decoded.mode).toBe("single");
  });

  test("a JSON array is a batch", () => {
    const decoded = decodeBody(JSON.stringify([event("a"), event("b")]), "application/json");
    expect(decoded.mode).toBe("batch");
    expect(decoded.items).toHaveLength(2);
  });

  test("NDJSON is a batch, tolerating CRLF, blank lines and a trailing newline", () => {
    const raw = `${JSON.stringify(event("a"))}\r\n\n${JSON.stringify(event("b"))}\n`;
    const decoded = decodeBody(raw, "application/json");
    expect(decoded.mode).toBe("batch");
    expect(decoded.items).toHaveLength(2);
  });

  test("an ndjson content-type forces line mode even for one line", () => {
    const decoded = decodeBody(JSON.stringify(event("a")), "application/x-ndjson");
    expect(decoded.mode).toBe("batch");
  });

  test("a UTF-8 BOM is stripped", () => {
    const decoded = decodeBody("﻿" + JSON.stringify(event("a")), "application/json");
    expect(decoded.mode).toBe("single");
  });

  test("bad lines are reported per index, not thrown", () => {
    const raw = [JSON.stringify(event("a")), "{not json", JSON.stringify(event("b"))].join("\n");
    const decoded = decodeBody(raw, "application/x-ndjson");
    expect(decoded.items[1]).toBeInstanceOf(BatchLineError);
    expect((decoded.items[1] as BatchLineError).index).toBe(1);
  });

  test("empty and whitespace-only bodies are rejected", () => {
    expect(() => decodeBody("", "application/json")).toThrow(BodyError);
    expect(() => decodeBody("   \n  ", "application/json")).toThrow(BodyError);
  });

  test("oversize batches are rejected", () => {
    const many = Array.from({ length: MAX_BATCH_ITEMS + 1 }, (_, i) => event(`k${i}`));
    expect(() => decodeBody(JSON.stringify(many), "application/json")).toThrow(BodyError);
  });
});

describe("POST /v1/events", () => {
  test("a single event returns the bare ingest result", async () => {
    const h = harness();
    const res = await h.post("/v1/events", event("single:1"));
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["inserted"]).toBe(true);
    expect(String(body["event_id"])).toStartWith("evt_");
  });

  test("a duplicate idempotency key returns the original id and does not re-insert", async () => {
    const h = harness();
    const first = (await (await h.post("/v1/events", event("dupe:1"))).json()) as Record<string, unknown>;
    const second = (await (await h.post("/v1/events", event("dupe:1"))).json()) as Record<string, unknown>;
    expect(second["inserted"]).toBe(false);
    expect(second["event_id"]).toBe(first["event_id"]);
    expect(h.events()).toHaveLength(1);
  });

  test("an NDJSON batch reports per-item results", async () => {
    const h = harness();
    const res = await h.post("/v1/events", ndjson(event("b:1"), event("b:2"), event("b:3")), {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { count: number; accepted: number; rejected: number; results: unknown[] };
    expect(body).toMatchObject({ count: 3, accepted: 3, rejected: 0 });
    expect(h.events()).toHaveLength(3);
  });

  test("one bad line does not discard the rest of the batch", async () => {
    const h = harness();
    const raw = [JSON.stringify(event("mix:1")), "{oops", JSON.stringify(event("mix:2"))].join("\n");
    const res = await h.post("/v1/events", raw, {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      accepted: number;
      rejected: number;
      results: Record<string, unknown>[];
    };
    expect(body.accepted).toBe(2);
    expect(body.rejected).toBe(1);
    expect(body.results[1]).toMatchObject({ index: 1, ok: false, error: "invalid_json" });
    expect(h.events()).toHaveLength(2);
  });

  test("a contract-invalid line is rejected alongside valid ones", async () => {
    const h = harness();
    const raw = ndjson(event("ok:1"), { contract: "car.event.v1", type: "nope" });
    const res = await h.post("/v1/events", raw, {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { accepted: number; results: Record<string, unknown>[] };
    expect(body.accepted).toBe(1);
    expect(body.results[1]).toMatchObject({ ok: false, error: "invalid_event" });
  });

  test("a batch where nothing was accepted is a 400", async () => {
    const h = harness();
    const res = await h.post("/v1/events", "{bad\n{alsobad", {
      headers: { "content-type": "application/x-ndjson" },
    });
    expect(res.status).toBe(400);
    expect((await res.json()) as { accepted: number }).toMatchObject({ accepted: 0 });
  });

  test("an invalid single event is a 400 with detail", async () => {
    const h = harness();
    const res = await h.post("/v1/events", { contract: "car.event.v1" });
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "invalid_event" });
    expect(h.events()).toHaveLength(0);
  });

  test("an empty body is a 400", async () => {
    const h = harness();
    const res = await h.post("/v1/events", "");
    expect(res.status).toBe(400);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ error: "empty_body" });
  });

  test("duplicates inside one batch collapse to a single row", async () => {
    const h = harness();
    const res = await h.post("/v1/events", ndjson(event("same"), event("same")), {
      headers: { "content-type": "application/x-ndjson" },
    });
    const body = (await res.json()) as { results: Record<string, unknown>[] };
    expect(body.results[0]).toMatchObject({ ok: true, inserted: true });
    expect(body.results[1]).toMatchObject({ ok: true, inserted: false });
    expect(h.events()).toHaveLength(1);
  });
});
