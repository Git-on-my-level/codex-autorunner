/**
 * HTTP test harness for the ingest loop.
 *
 * The app is driven through `app.fetch(request, env)` rather than a real socket.
 * `env` stands in for Bun's `Server`: auth.ts reads the peer address from
 * `env.requestIP(req)`, which is exactly what Bun provides in production, so
 * localhost-vs-remote is exercised for real without binding a port.
 */
import { Hono } from "hono";
import type { CarConfig } from "../../src/config/config.ts";
import type { DaemonDeps, MemoryWriter, TriagePort } from "../../src/ports.ts";
import type { Store } from "../../src/store/db.ts";
import { createIngestApp, type IngestOptions } from "../../src/ingest/server.ts";
import {
  AllowAllPolicy,
  EmptyMemoryReader,
  FakeActionBus,
  FakeChannel,
  FakeClock,
  memoryStore,
  testConfig,
} from "../fakes.ts";

export const LOCALHOST = "127.0.0.1";
export const REMOTE = "203.0.113.9";

class NoopTriage implements TriagePort {
  async tick(): Promise<number> {
    return 0;
  }
}

class NoopMemoryWriter implements MemoryWriter {
  addFromDavid(): string {
    return "mem_test";
  }
  propose(): string {
    return "mem_test";
  }
  recordOutcome(): void {}
  setAutonomy(): void {}
}

export interface Harness {
  app: Hono;
  store: Store;
  clock: FakeClock;
  config: CarConfig;
  /** POST a body to `path`, as if from `from` (default: localhost). */
  post(
    path: string,
    body: string | unknown,
    init?: { from?: string | null; headers?: Record<string, string> },
  ): Promise<Response>;
  get(path: string, init?: { from?: string | null }): Promise<Response>;
  /** Rows in the audit table, newest last. */
  audits(verb?: string): { verb: string; object_id: string; detail_json: string }[];
  events(): { id: string; idempotency_key: string; type: string; severity: string }[];
  /** Resolve an event id once ingest has committed it (used to answer a park). */
  waitForEventId(idempotencyKey: string, timeoutMs?: number): Promise<string>;
}

export function harness(
  configOverrides: Record<string, unknown> = {},
  opts: IngestOptions = {},
  extraApps: { path: string; app: Hono }[] = [],
): Harness {
  const clock = new FakeClock();
  const store = memoryStore(clock);
  const config = testConfig(configOverrides);

  const deps: DaemonDeps = {
    store,
    config,
    triage: new NoopTriage(),
    actions: new FakeActionBus(),
    channel: new FakeChannel(),
    memoryReader: new EmptyMemoryReader(),
    memoryWriter: new NoopMemoryWriter(),
    policy: new AllowAllPolicy(),
  };

  const app = createIngestApp(deps, extraApps, opts);

  /** `null` means "peer address unknown" — the fail-closed path. */
  const envFor = (from: string | null | undefined) => {
    const address = from === undefined ? LOCALHOST : from;
    if (address === null) return {};
    return { requestIP: () => ({ address, family: "IPv4", port: 54321 }) };
  };

  return {
    app,
    store,
    clock,
    config,
    post(path, body, init = {}) {
      const text = typeof body === "string" ? body : JSON.stringify(body);
      const req = new Request(`http://127.0.0.1:7171${path}`, {
        method: "POST",
        headers: { "content-type": "application/json", ...(init.headers ?? {}) },
        body: text,
      });
      return Promise.resolve(app.fetch(req, envFor(init.from)));
    },
    get(path, init = {}) {
      const req = new Request(`http://127.0.0.1:7171${path}`);
      return Promise.resolve(app.fetch(req, envFor(init.from)));
    },
    audits(verb) {
      const rows = store.db
        .query("SELECT verb, object_id, detail_json FROM audit ORDER BY id ASC")
        .all() as { verb: string; object_id: string; detail_json: string }[];
      return verb ? rows.filter((r) => r.verb === verb) : rows;
    },
    events() {
      return store.db
        .query("SELECT id, idempotency_key, type, severity FROM events ORDER BY received_at ASC, id ASC")
        .all() as { id: string; idempotency_key: string; type: string; severity: string }[];
    },
    async waitForEventId(idempotencyKey, timeoutMs = 2000) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const row = store.db
          .query("SELECT id FROM events WHERE idempotency_key = ?")
          .get(idempotencyKey) as { id: string } | null;
        if (row) return row.id;
        if (Date.now() > deadline) throw new Error(`event ${idempotencyKey} never committed`);
        await Bun.sleep(1);
      }
    },
  };
}

/** Read a fixture's `input` payload by source + filename prefix. */
export function fixtureInput(source: string, filePrefix: string): unknown {
  const { readdirSync, readFileSync } = require("node:fs") as typeof import("node:fs");
  const dir = `${import.meta.dir}/../fixtures/${source}`;
  const file = readdirSync(dir).find((f) => f.startsWith(filePrefix));
  if (!file) throw new Error(`no fixture ${source}/${filePrefix}*`);
  return JSON.parse(readFileSync(`${dir}/${file}`, "utf8")).input;
}
