/**
 * WS-A owns src/ingest/: this Hono server plus per-source normalizers
 * (agentctl, claude hooks, multica, generic, telegram-note).
 * Scaffold ships generic ingest + health + schema; WS-A extends.
 */
import { Hono } from "hono";
import type { DaemonDeps, Loop } from "../ports.ts";
import { parseEvent, CarEvent } from "../contract/events.ts";

export function createIngestServer(deps: DaemonDeps, extraApps: { path: string; app: Hono }[] = []): Loop {
  const app = new Hono();
  let server: ReturnType<typeof Bun.serve> | null = null;

  app.get("/healthz", (c) => c.json({ ok: true }));
  app.get("/v1/schema", (c) => c.json(CarEvent.def ?? { contract: "car.event.v1" }));

  app.post("/v1/events", async (c) => {
    const raw = await c.req.text();
    const lines = raw.trimStart().startsWith("{") && raw.includes("\n")
      ? raw.split("\n").filter((l) => l.trim().length > 0)
      : [raw];
    const results = [];
    for (const line of lines) {
      let parsed;
      try {
        parsed = parseEvent(JSON.parse(line));
      } catch (err) {
        return c.json({ error: "invalid_event", detail: String(err) }, 400);
      }
      results.push(deps.store.ingestEvent(parsed));
    }
    return c.json(results.length === 1 ? results[0] : results, 200);
  });

  for (const extra of extraApps) app.route(extra.path, extra.app);

  return {
    name: "http",
    start() {
      server = Bun.serve({
        hostname: deps.config.http.host,
        port: deps.config.http.port,
        fetch: app.fetch,
      });
    },
    stop() {
      server?.stop();
    },
  };
}
