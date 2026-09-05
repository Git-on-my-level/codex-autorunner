/** A transport adapter, not a second authority model. No callbacks, shell, or client-supplied tenant. */
import { REQUEST_GUIDE } from "./guidance.ts";
import { Hono, type Context } from "hono";
import { z } from "zod";
import type { CarConfig } from "../config/config.ts";
import { bearerToken, timingSafeEqual, configuredTokens } from "../ingest/auth.ts";
import { AcknowledgeRequest, CancelRequest, DecisionPacket, EnrichRequest, RaiseRequest, type ClientIdentity } from "./contract.ts";
import { AttentionError } from "./errors.ts";
import type { AttentionService } from "./service.ts";

export const MAX_REQUEST_BYTES = 64 * 1024;
export function attentionIdentity(c: Context, config: CarConfig): ClientIdentity | null {
  const token = bearerToken(c.req.header("authorization"));
  if (!token) return null;
  for (const [clientId, client] of Object.entries(config.attention.clients)) {
    const expected = process.env[client.token_env];
    if (expected && timingSafeEqual(token, expected)) return { workspaceId: config.attention.workspace_id, clientId, host: client.host };
  }
  return null;
}
/** Prevent an accidentally reused token from becoming both an agent and its human approver. */
export function validateAttentionCredentials(config: CarConfig): void {
  const used = new Set<string>();
  const humanTokens = configuredTokens(config, "web");
  for (const source of ["generic", "agentctl", "claude", "multica", "provider"] as const) {
    if (configuredTokens(config, source).some((token) => humanTokens.includes(token)))
      throw new Error(`Separate human web credentials from ${source} credentials. A wildcard/shared ingest token must not authorize human decisions.`);
  }
  for (const [id, client] of Object.entries(config.attention.clients)) {
    const token = process.env[client.token_env];
    if (!token || token.length < 32) throw new Error(`attention client ${id}: ${client.token_env} must contain at least 32 characters`);
    if (used.has(token) || humanTokens.includes(token)) throw new Error(`attention client ${id}: use a distinct agent credential, never a human/shared credential`);
    used.add(token);
  }
  if (config.attention.triage_enabled && !config.attention.triage_model) throw new Error("attention.triage_model is required when triage_enabled=true");
}

export async function readJsonBounded(c: Context): Promise<unknown> {
  if (!c.req.header("content-type")?.toLowerCase().startsWith("application/json"))
    throw new AttentionError("content_type", "Use application/json", 400);
  const reader = c.req.raw.body?.getReader();
  if (!reader) throw new AttentionError("invalid_json", "JSON body required", 400);
  const chunks: Uint8Array[] = []; let total = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      total += value.byteLength;
      if (total > MAX_REQUEST_BYTES) { await reader.cancel(); throw new AttentionError("body_too_large", "Request exceeds 64 KiB", 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const joined = new Uint8Array(total); let offset = 0;
  for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.length; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(joined)); }
  catch { throw new AttentionError("invalid_json", "Malformed UTF-8 JSON", 400); }
}

export function createAttentionApi(service: AttentionService): { path: string; app: Hono } {
  const app = new Hono();
  app.onError((error, c) => {
    if (error instanceof AttentionError) return c.json({ error: error.code, message: error.message }, error.status);
    if (error instanceof z.ZodError) return c.json({ error: "invalid_request", issues: error.issues }, 400);
    service.store.audit("api", "attention.request_failed", "path", c.req.path, { error: String(error) });
    return c.json({ error: "internal_error", message: "The request was not confirmed. Retry with the same idempotency key." }, 500);
  });
  app.get("/schema", (c) => c.json({ contract: "car.request.v1", schema: z.toJSONSchema(RaiseRequest), packet: z.toJSONSchema(DecisionPacket),
    instructions: "Raise one durable request. Add context when requested. Never infer approval from silence. GET is not acknowledgement. Agent credentials cannot answer decisions." }));
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "no-store");
    if (!attentionIdentity(c, service.config)) return c.json({ error: "unauthorized" }, 401, { "WWW-Authenticate": 'Bearer realm="car-attention"' });
    await next();
  });
  app.get("/capabilities", (c) => c.json(REQUEST_GUIDE));
  app.post("/requests", async (c) => {
    const input = RaiseRequest.parse(await readJsonBounded(c));
    const row = service.raise(attentionIdentity(c, service.config)!, input.idempotency_key, input.packet);
    return c.json(service.view(row), 200);
  });
  app.get("/requests", (c) => {
    const rows = service.list(attentionIdentity(c, service.config)!, c.req.query("before"));
    const page = rows.slice(0, 100);
    return c.json({ requests: page.map((r) => service.summary(r)), next_cursor: rows.length > 100 ? service.cursor(page.at(-1)!) : null });
  });
  app.get("/requests/:id", (c) => {
    const row = service.owned(c.req.param("id"), attentionIdentity(c, service.config)!);
    // Last-seen is an observation, not proof of continued execution or resolution.
    const now = service.store.clock.now().toISOString();
    service.store.db.query("UPDATE attention_requests SET last_seen_at=? WHERE id=?").run(now, row.id);
    return c.json(service.view({ ...row, last_seen_at: now }));
  });
  app.post("/requests/:id/context", async (c) => {
    const input = EnrichRequest.parse(await readJsonBounded(c));
    return c.json(service.view(service.enrich(attentionIdentity(c, service.config)!, c.req.param("id"), input.expected_revision, input.packet)));
  });
  app.post("/requests/:id/ack", async (c) => {
    const input = AcknowledgeRequest.parse(await readJsonBounded(c));
    return c.json(service.view(service.acknowledge(attentionIdentity(c, service.config)!, c.req.param("id"), input.answer_id, input.outcome, input.note)));
  });
  app.post("/requests/:id/cancel", async (c) => {
    const input = CancelRequest.parse(await readJsonBounded(c));
    return c.json(service.view(service.cancel(attentionIdentity(c, service.config)!, c.req.param("id"), input.expected_revision, input.reason)));
  });
  return { path: "/v1/attention", app };
}
