#!/usr/bin/env bun
/**
 * `card` — CAR v3 CLI.
 * Scaffold implements: serve, emit, status, doctor(basic). WS-G extends docs;
 * memory/policy subcommands land with WS-C/WS-B.
 */
import { startDaemon } from "./daemon.ts";
import { loadConfig, dbPath } from "./config/config.ts";
import { openStore } from "./store/db.ts";
import { CONTRACT_VERSION, computedIdempotencyKey, parseEvent } from "./contract/events.ts";
import { hostname } from "node:os";

function argValue(args: string[], flag: string): string | undefined {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : undefined;
}

const [cmd, ...rest] = process.argv.slice(2);

switch (cmd) {
  case "serve": {
    const daemon = await startDaemon(argValue(rest, "--config"));
    const shutdown = async () => {
      await daemon.stop();
      process.exit(0);
    };
    process.on("SIGINT", shutdown);
    process.on("SIGTERM", shutdown);
    console.log("card: daemon running");
    break;
  }

  case "emit": {
    // card emit --type attention.error --title "..." [--body ...] [--severity ...] [--port 7171]
    const type = argValue(rest, "--type") ?? "note";
    const now = new Date();
    const payload = { via: "card-emit" };
    const event = parseEvent({
      contract: CONTRACT_VERSION,
      idempotency_key:
        argValue(rest, "--idempotency-key") ?? computedIdempotencyKey("cron", type, payload, now),
      ts: now.toISOString(),
      source: { vendor: "cron", host: hostname(), adapter: "card-emit" },
      session: null,
      type,
      severity: argValue(rest, "--severity") ?? "notice",
      title: argValue(rest, "--title") ?? "",
      body: argValue(rest, "--body") ?? "",
      payload,
    });
    const port = argValue(rest, "--port") ?? String(loadConfig().http.port);
    const res = await fetch(`http://127.0.0.1:${port}/v1/events`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(event),
    });
    console.log(JSON.stringify(await res.json()));
    if (!res.ok) process.exit(1);
    break;
  }

  case "status": {
    const cfg = loadConfig(argValue(rest, "--config"));
    const store = openStore(dbPath(cfg));
    const q = (sql: string) => (store.db.query(sql).get() as { n: number }).n;
    console.log(
      JSON.stringify(
        {
          db: dbPath(cfg),
          events: q("SELECT COUNT(*) n FROM events"),
          pending_triage: q("SELECT COUNT(*) n FROM events WHERE triage_state IN ('pending','coalescing')"),
          open_escalations: q("SELECT COUNT(*) n FROM escalations WHERE state = 'pending'"),
          active_sessions: q("SELECT COUNT(*) n FROM sessions WHERE state = 'active'"),
        },
        null,
        2,
      ),
    );
    break;
  }

  case "doctor": {
    const cfg = loadConfig(argValue(rest, "--config"));
    const checks: [string, boolean, string][] = [];
    checks.push(["state_dir", true, cfg.state_dir]);
    try {
      openStore(dbPath(cfg)).db.close();
      checks.push(["sqlite", true, dbPath(cfg)]);
    } catch (err) {
      checks.push(["sqlite", false, String(err)]);
    }
    checks.push([
      "telegram",
      !cfg.telegram.enabled || Boolean(process.env[cfg.telegram.token_env]),
      cfg.telegram.enabled ? `token via $${cfg.telegram.token_env}` : "disabled",
    ]);
    /*
     * Without a provider key CAR still runs, and still fails safe — but every
     * event the rules pass cannot settle escalates, which looks like a busy day
     * rather than a broken install. Name it here instead.
     */
    const providerEnv = cfg.providers.triage.startsWith("openai/") ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY";
    checks.push([
      "triage llm",
      Boolean(process.env[providerEnv]),
      process.env[providerEnv]
        ? `${cfg.providers.triage} via $${providerEnv}`
        : `$${providerEnv} not set — triage escalates everything the rules pass cannot settle`,
    ]);
    for (const [name, ok, detail] of checks) console.log(`${ok ? "ok " : "FAIL"} ${name}: ${detail}`);
    process.exit(checks.every(([, ok]) => ok) ? 0 : 1);
  }

  default:
    console.log("card <serve|emit|status|doctor> — CAR v3 (see v3/DESIGN.md)");
    process.exit(cmd ? 1 : 0);
}
