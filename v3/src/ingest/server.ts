/**
 * WS-A owns src/ingest/: the Hono HTTP loop plus per-source normalizers
 * (agentctl, claude hooks, multica, generic).
 *
 * Routes:
 *   GET  /healthz
 *   GET  /v1/schema              JSON Schema for car.event.v1 (derived from zod)
 *   POST /v1/events              canonical envelope; single object, JSON array, or NDJSON batch
 *   POST /v1/ingest/agentctl     agentctl `subscribe` webhook deliveries
 *   POST /v1/ingest/claude       Claude Code hook HTTP payloads (parks PermissionRequest)
 *   POST /v1/ingest/multica      Multica issue/card webhooks
 *
 * Auth: localhost is trusted; every other peer needs a per-source bearer token
 * from config.http.ingest_tokens (see auth.ts — it fails closed).
 */
import { Hono, type Context } from "hono";
import type { DaemonDeps, Loop } from "../ports.ts";
import type { CarEvent } from "../contract/events.ts";
import type { IngestResult } from "../store/db.ts";
import { parseEvent } from "../contract/events.ts";
import { hasParkedPermission, parkPermission, releaseAllParks } from "../permission_park.ts";
import { authorize, type SourceId } from "./auth.ts";
import { BatchLineError, BodyError, decodeBody } from "./batch.ts";
import { eventJsonSchema } from "./schema.ts";
import { makeContext, NormalizeError } from "./normalize.ts";
import { normalizeAgentctl } from "./agentctl.ts";
import {
  DEFAULT_HOOK_TIMEOUT_MS,
  hookDecisionBody,
  normalizeClaude,
  NO_DECISION_BODY,
  parkDeadlineMs,
  readHookTimeoutHint,
} from "./claude.ts";
import { normalizeMultica } from "./multica.ts";

export interface IngestOptions {
  /**
   * Hook timeout used when a Claude PermissionRequest ships no hint. Tests set a
   * short value to exercise the park-timeout path without waiting a minute.
   */
  defaultHookTimeoutMs?: number;
}

/* ------------------------------------------------------------------- app */

/**
 * Build the Hono app without binding a socket. `createIngestServer` wraps this;
 * tests drive `app.fetch(request, fakeServer)` directly.
 */
export function createIngestApp(
  deps: DaemonDeps,
  extraApps: { path: string; app: Hono }[] = [],
  opts: IngestOptions = {},
): Hono {
  const app = new Hono();

  app.get("/healthz", (c) => c.json({ ok: true, contract: "car.event.v1" }));

  app.get("/v1/schema", (c) => c.json(eventJsonSchema()));

  /* ------------------------------------------------------ generic envelope */

  app.post("/v1/events", async (c) => {
    const denied = guard(c, deps, "generic");
    if (denied) return denied;

    let decoded;
    try {
      decoded = decodeBody(await c.req.text(), c.req.header("content-type"));
    } catch (err) {
      if (err instanceof BodyError) return c.json({ error: err.code, detail: err.message }, 400);
      throw err;
    }

    if (decoded.mode === "single") {
      let event: CarEvent;
      try {
        event = parseEvent(decoded.items[0]);
      } catch (err) {
        return c.json({ error: "invalid_event", detail: describeError(err) }, 400);
      }
      return c.json(deps.store.ingestEvent(event), 200);
    }

    // Batch: one bad line must not discard the good ones (at-least-once
    // producers replay whole batches, and losing 999 events to fix 1 is worse).
    const results: Record<string, unknown>[] = [];
    let accepted = 0;
    let rejected = 0;
    decoded.items.forEach((item, index) => {
      if (item instanceof BatchLineError) {
        rejected++;
        results.push({ index, ok: false, error: "invalid_json", detail: item.detail });
        return;
      }
      try {
        const result = deps.store.ingestEvent(parseEvent(item));
        accepted++;
        results.push({ index, ok: true, ...result });
      } catch (err) {
        rejected++;
        results.push({ index, ok: false, error: "invalid_event", detail: describeError(err) });
      }
    });

    return c.json(
      { contract: "car.event.v1", count: results.length, accepted, rejected, results },
      accepted === 0 ? 400 : 200,
    );
  });

  /* ----------------------------------------------------------- agentctl */

  app.post("/v1/ingest/agentctl", async (c) => {
    const denied = guard(c, deps, "agentctl");
    if (denied) return denied;

    let decoded;
    try {
      decoded = decodeBody(await c.req.text(), c.req.header("content-type"));
    } catch (err) {
      if (err instanceof BodyError) return c.json({ error: err.code, detail: err.message }, 400);
      throw err;
    }

    const ctx = makeContext(deps.store.clock.now());
    const results: IngestResult[] = [];
    const errors: Record<string, unknown>[] = [];

    for (const [index, item] of decoded.items.entries()) {
      if (item instanceof BatchLineError) {
        errors.push({ index, error: "invalid_json", detail: item.detail });
        continue;
      }
      try {
        for (const event of normalizeAgentctl(item, ctx)) {
          results.push(deps.store.ingestEvent(event));
        }
      } catch (err) {
        errors.push({ index, error: "invalid_agentctl_event", detail: describeError(err) });
      }
    }

    if (results.length === 0) {
      return c.json({ error: "invalid_agentctl_event", errors }, 400);
    }
    return c.json(
      { accepted: results.length, rejected: errors.length, results, ...(errors.length ? { errors } : {}) },
      200,
    );
  });

  /* ------------------------------------------------------------- claude */

  app.post("/v1/ingest/claude", async (c) => {
    const denied = guard(c, deps, "claude");
    if (denied) return denied;

    let raw: unknown;
    try {
      raw = JSON.parse(await c.req.text());
    } catch (err) {
      return c.json({ error: "invalid_json", detail: describeError(err) }, 400);
    }

    /*
     * Resolve the hook timeout ONCE, here, and hand the same number to the
     * normalizer: the deadline_ms recorded on the event and the deadline the
     * response is actually parked for must be the same number, or WS-E's
     * adapters would race a deadline that never existed.
     */
    const hookTimeoutMs =
      readHookTimeoutHint(
        (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>,
        c.req.raw.headers,
        safeUrl(c.req.url),
      ) ??
      opts.defaultHookTimeoutMs ??
      DEFAULT_HOOK_TIMEOUT_MS;

    let normalized;
    try {
      normalized = normalizeClaude(raw, makeContext(deps.store.clock.now()), { hookTimeoutMs });
    } catch (err) {
      return c.json({ error: "invalid_claude_hook", detail: describeError(err) }, 400);
    }

    const result = deps.store.ingestEvent(normalized.event);

    if (!normalized.park) {
      // Non-decision hooks: ACK only after the insert committed, with no body
      // that Claude would read as a decision.
      return c.json({ ok: true, ...result }, 200);
    }

    /*
     * PermissionRequest: hold the HTTP response open while triage / the
     * Telegram button race the hook deadline. On a decision, answer in-band;
     * on timeout, return an empty 200 so Claude falls back to its local prompt.
     */
    if (hasParkedPermission(result.event_id)) {
      // A redelivery of a request already parked. Parking twice would orphan the
      // first promise (the registry is keyed by event id), so let the in-flight
      // request own the decision and let this one degrade immediately.
      return c.json(NO_DECISION_BODY, 200);
    }

    const deadline = parkDeadlineMs(hookTimeoutMs);

    deps.store.audit("daemon", "permission.parked", "event", result.event_id, {
      deadline_ms: deadline,
      tool_use_id: normalized.event.response_channel?.hint?.["tool_use_id"] ?? null,
    });

    const decision = await parkPermission(result.event_id, deadline);

    deps.store.audit("daemon", decision ? "permission.answered" : "permission.timed_out", "event", result.event_id, {
      decision: decision?.decision ?? null,
    });

    return c.json(decision ? hookDecisionBody(decision) : NO_DECISION_BODY, 200);
  });

  /* ------------------------------------------------------------ multica */

  app.post("/v1/ingest/multica", async (c) => {
    const denied = guard(c, deps, "multica");
    if (denied) return denied;

    let decoded;
    try {
      decoded = decodeBody(await c.req.text(), c.req.header("content-type"));
    } catch (err) {
      if (err instanceof BodyError) return c.json({ error: err.code, detail: err.message }, 400);
      throw err;
    }

    const ctx = makeContext(deps.store.clock.now());
    const results: IngestResult[] = [];
    const errors: Record<string, unknown>[] = [];

    for (const [index, item] of decoded.items.entries()) {
      if (item instanceof BatchLineError) {
        errors.push({ index, error: "invalid_json", detail: item.detail });
        continue;
      }
      try {
        results.push(deps.store.ingestEvent(normalizeMultica(item, ctx)));
      } catch (err) {
        errors.push({ index, error: "invalid_multica_event", detail: describeError(err) });
      }
    }

    if (results.length === 0) return c.json({ error: "invalid_multica_event", errors }, 400);
    return c.json(
      { accepted: results.length, rejected: errors.length, results, ...(errors.length ? { errors } : {}) },
      200,
    );
  });

  for (const extra of extraApps) app.route(extra.path, extra.app);

  // DESIGN §9: /brief.md lives at the daemon root for agents to curl; the web
  // UI serves the same markdown under its own prefix. Alias, not a redirect,
  // so `curl` needs no -L.
  app.get("/brief.md", async (c) => {
    const { buildBriefMarkdown } = await import("../surfaces/web/brief.ts");
    return c.text(buildBriefMarkdown(deps.store), 200, {
      "content-type": "text/markdown; charset=utf-8",
    });
  });

  return app;
}

/* ----------------------------------------------------------------- loop */

export function createIngestServer(
  deps: DaemonDeps,
  extraApps: { path: string; app: Hono }[] = [],
  opts: IngestOptions = {},
): Loop {
  const app = createIngestApp(deps, extraApps, opts);
  let server: ReturnType<typeof Bun.serve> | null = null;

  return {
    name: "http",
    start() {
      server = Bun.serve({
        hostname: deps.config.http.host,
        port: deps.config.http.port,
        // Bun's second argument is the Server; Hono exposes it as c.env, which is
        // how auth.ts reads the real peer address (never a proxy header).
        fetch: (req, bunServer) => app.fetch(req, bunServer),
      });
    },
    stop() {
      // Release parks BEFORE stopping the server: Bun drains in-flight requests
      // on stop, and a parked PermissionRequest would otherwise hold shutdown
      // open for its full deadline. Released parks return "no decision", so
      // Claude falls back to its local prompt — degraded, never hung.
      releaseAllParks();
      server?.stop();
      server = null;
    },
  };
}

/* -------------------------------------------------------------- helpers */

function guard(c: Context, deps: DaemonDeps, source: SourceId): Response | null {
  const verdict = authorize(c, deps.config, source);
  if (verdict.ok) return null;
  deps.store.audit("daemon", "ingest.denied", "source", source, { reason: verdict.reason });
  return c.json({ error: "unauthorized", detail: verdict.reason }, 401, {
    "WWW-Authenticate": `Bearer realm="car-ingest", scope="${source}"`,
  });
}

function describeError(err: unknown): string {
  if (err instanceof NormalizeError) return err.message;
  if (err instanceof Error) return `${err.name}: ${err.message}`;
  return String(err);
}

function safeUrl(url: string): URL | undefined {
  try {
    return new URL(url);
  } catch {
    return undefined;
  }
}
