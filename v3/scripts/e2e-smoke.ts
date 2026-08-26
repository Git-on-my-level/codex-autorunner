#!/usr/bin/env bun
/**
 * v3/scripts/e2e-smoke.ts
 *
 * End-to-end smoke test: boots a full `card serve` daemon (startDaemon) against a
 * throwaway config (random free-ish port, temp state_dir), replays a small battery
 * of events over real HTTP, asserts against /healthz and the web UI's brief
 * endpoint, then re-opens the SQLite file read-only and asserts directly on the
 * store: event count, idempotency dedupe, session creation, audit completeness.
 *
 * Deliberately scoped to only the generic `POST /v1/events` path and the scaffold
 * `/healthz` route plus the store's own tables — all guaranteed present from the
 * Phase 0 scaffold regardless of whether the other workstreams' modules (triage,
 * memory, telegram, actions, web UI) are still no-op stubs or fully implemented.
 * It does NOT assert on triage decisions, escalations, or Telegram delivery — those
 * are each workstream's own test suites' job.
 *
 * Not wired into package.json (package.json is frozen for this build). Run directly:
 *   bun run scripts/e2e-smoke.ts
 * Exits non-zero if any check fails.
 */
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { startDaemon } from "../src/daemon.ts";
import { CONTRACT_VERSION } from "../src/contract/events.ts";
import type { CarEvent } from "../src/contract/events.ts";

interface CheckResult {
  name: string;
  pass: boolean;
  detail?: string;
}
const results: CheckResult[] = [];
function check(name: string, pass: boolean, detail?: string): void {
  results.push({ name, pass, detail });
  console.log(`${pass ? "ok  " : "FAIL"} ${name}${detail !== undefined ? ` — ${detail}` : ""}`);
}

function randomPort(): number {
  return 20000 + Math.floor(Math.random() * 20000);
}

async function post(base: string, ev: CarEvent): Promise<{ ok: boolean; status: number; body: unknown }> {
  const res = await fetch(`${base}/v1/events`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(ev),
  });
  const body = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, body };
}

async function main(): Promise<void> {
  const stateDirParent = mkdtempSync(join(tmpdir(), "car-e2e-"));
  const stateDir = join(stateDirParent, "state");
  const configPath = join(stateDirParent, "config.toml");
  const port = randomPort();
  writeFileSync(
    configPath,
    [`state_dir = ${JSON.stringify(stateDir)}`, "", "[http]", 'host = "127.0.0.1"', `port = ${port}`, ""].join("\n"),
  );
  console.log(`e2e-smoke: state_dir=${stateDir} port=${port}`);

  const base = `http://127.0.0.1:${port}`;
  const daemon = await startDaemon(configPath);
  const sessionNativeId = `e2e-sess-${Date.now()}`;

  try {
    // GET /healthz
    const health = (await fetch(`${base}/healthz`)
      .then((r) => r.json())
      .catch(() => null)) as { ok?: boolean } | null;
    check("GET /healthz -> {ok:true}", health?.ok === true, JSON.stringify(health));

    const ts = () => new Date().toISOString();

    const noteEvent: CarEvent = {
      contract: CONTRACT_VERSION,
      idempotency_key: "e2e:note:1",
      ts: ts(),
      source: { vendor: "other", host: "e2e-host", adapter: "e2e-smoke" },
      session: null,
      type: "note",
      severity: "info",
      requires_response: false,
      response_channel: null,
      title: "smoke note",
      body: "hello from e2e-smoke",
      payload: {},
    };

    const questionEvent: CarEvent = {
      contract: CONTRACT_VERSION,
      idempotency_key: `e2e:question:${sessionNativeId}:1`,
      ts: ts(),
      source: { vendor: "other", host: "e2e-host", adapter: "e2e-smoke" },
      session: {
        vendor: "other",
        native_id: sessionNativeId,
        host: "e2e-host",
        cwd: "/tmp/e2e",
        title: "e2e smoke session",
      },
      type: "attention.question",
      severity: "attention",
      requires_response: true,
      response_channel: { kind: "file" },
      title: "which way?",
      body: "pick one",
      payload: {},
    };

    const heartbeatEvent: CarEvent = {
      contract: CONTRACT_VERSION,
      idempotency_key: `e2e:heartbeat:${sessionNativeId}:1`,
      ts: ts(),
      source: { vendor: "other", host: "e2e-host", adapter: "e2e-smoke" },
      session: {
        vendor: "other",
        native_id: sessionNativeId,
        host: "e2e-host",
      },
      type: "heartbeat",
      severity: "info",
      requires_response: false,
      response_channel: null,
      title: "",
      body: "",
      payload: {},
    };

    const r1 = await post(base, noteEvent);
    const r1body = r1.body as { inserted?: boolean; event_id?: string } | null;
    check("POST generic note -> 200, inserted:true", r1.ok && r1body?.inserted === true, JSON.stringify(r1));

    const r2 = await post(base, questionEvent);
    const r2body = r2.body as { inserted?: boolean; car_session_id?: string | null } | null;
    check(
      "POST sessionful attention.question -> 200, inserted:true, car_session_id set",
      r2.ok && r2body?.inserted === true && Boolean(r2body?.car_session_id),
      JSON.stringify(r2),
    );

    const r3 = await post(base, heartbeatEvent);
    const r3body = r3.body as { inserted?: boolean } | null;
    check("POST heartbeat (same session) -> 200, inserted:true", r3.ok && r3body?.inserted === true, JSON.stringify(r3));

    // Duplicate idempotency_key: must NOT create a new row; must return the original event_id.
    const r1dup = await post(base, noteEvent);
    const r1dupBody = r1dup.body as { inserted?: boolean; event_id?: string } | null;
    check(
      "duplicate idempotency_key -> 200, inserted:false, same event_id",
      r1dup.ok && r1dupBody?.inserted === false && r1dupBody?.event_id === r1body?.event_id,
      JSON.stringify(r1dup),
    );

    // brief.md: DESIGN.md specifies GET /brief.md at the daemon root; the current
    // scaffold web-UI stub mounts it under /ui. Try both so this test survives
    // whichever mount point WS-F ships.
    let briefRes = await fetch(`${base}/brief.md`);
    if (!briefRes.ok) briefRes = await fetch(`${base}/ui/brief.md`);
    const briefText = await briefRes.text().catch(() => "");
    check(
      "GET (/brief.md or /ui/brief.md) -> 200 non-empty markdown",
      briefRes.ok && briefText.length > 0,
      `url=${briefRes.url} status=${briefRes.status} len=${briefText.length}`,
    );

    // Let the triage loop tick at least once before we stop the daemon (it polls
    // every 2s per daemon.ts); not asserted on directly, just gives any real
    // (non-stub) triage/outbox implementation a chance to run before we snapshot.
    await Bun.sleep(2200);
  } finally {
    await daemon.stop();
  }

  // Direct SQLite assertions, read-only, after the daemon (and its WAL writers)
  // have fully closed.
  const dbPath = join(stateDir, "car.db");
  const db = new Database(dbPath, { readonly: true });
  try {
    const eventCount = (db.query("SELECT COUNT(*) n FROM events").get() as { n: number }).n;
    check("events table has exactly 3 rows (dedupe collapsed the 4th POST)", eventCount === 3, `count=${eventCount}`);

    const dedupeCount = (
      db.query("SELECT COUNT(*) n FROM events WHERE idempotency_key = ?").get("e2e:note:1") as { n: number }
    ).n;
    check("no duplicate row for the reused idempotency_key", dedupeCount === 1, `count=${dedupeCount}`);

    const sessionCount = (db.query("SELECT COUNT(*) n FROM sessions").get() as { n: number }).n;
    check(
      "sessions table has exactly 1 row (question + heartbeat resolved to one session)",
      sessionCount === 1,
      `count=${sessionCount}`,
    );

    const heartbeatSession = db
      .query("SELECT last_heartbeat_at FROM sessions WHERE car_session_id = (SELECT car_session_id FROM session_refs WHERE native_id = ?)")
      .get(sessionNativeId) as { last_heartbeat_at: string | null } | null;
    check(
      "session's last_heartbeat_at was updated by the heartbeat event",
      Boolean(heartbeatSession?.last_heartbeat_at),
      JSON.stringify(heartbeatSession),
    );

    const auditCount = (db.query("SELECT COUNT(*) n FROM audit").get() as { n: number }).n;
    check("audit table is non-empty", auditCount > 0, `count=${auditCount}`);

    const verbs = new Set((db.query("SELECT DISTINCT verb FROM audit").all() as { verb: string }[]).map((v) => v.verb));
    check(
      "audit contains event.ingested and session.created rows",
      verbs.has("event.ingested") && verbs.has("session.created"),
      JSON.stringify([...verbs]),
    );

    const daemonAudit = new Set(
      (db.query("SELECT verb FROM audit WHERE object_type = 'daemon'").all() as { verb: string }[]).map((v) => v.verb),
    );
    check("audit records daemon.started", daemonAudit.has("daemon.started"), JSON.stringify([...daemonAudit]));
  } finally {
    db.close();
  }

  try {
    rmSync(stateDirParent, { recursive: true, force: true });
  } catch {
    // best-effort cleanup; leaving a stray /tmp dir is not a test failure.
  }

  const failed = results.filter((r) => !r.pass);
  console.log(`\ne2e-smoke: ${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length > 0) {
    console.error(`e2e-smoke: FAILED — ${failed.map((f) => f.name).join("; ")}`);
    process.exit(1);
  }
  console.log("e2e-smoke: all checks passed");
}

main().catch((err) => {
  console.error("e2e-smoke: uncaught error", err);
  process.exit(1);
});
