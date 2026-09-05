#!/usr/bin/env bun
import { ATTENTION_COMMANDS, ATTENTION_HELP, runAttentionCli } from "./attention/cli.ts";
/** `card` — CAR v3 daemon, authenticated event emitter, status, and diagnostics. */
import { startDaemon } from "./daemon.ts";
import { loadConfig, dbPath } from "./config/config.ts";
import { validateProviderTopology } from "./config/provider_topology.ts";
import { openStore } from "./store/db.ts";
import { CONTRACT_VERSION, computedIdempotencyKey, parseEvent } from "./contract/events.ts";
import { hostname } from "node:os";
import { mkdirSync } from "node:fs";

function argValue(args: string[], flag: string): string | undefined {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : undefined;
}

const [cmd, ...rest] = process.argv.slice(2);

if (cmd && ATTENTION_COMMANDS.has(cmd)) {
  try { await runAttentionCli(cmd, rest); }
  catch (error) {
    const result = { error: (error as { code?: string }).code ?? "command_failed", message: error instanceof Error ? error.message : String(error) };
    // MCP stdout is reserved exclusively for JSON-RPC.
    if (cmd === "mcp") console.error(JSON.stringify(result)); else console.log(JSON.stringify(result));
    process.exitCode = 1;
  }
} else switch (cmd) {
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
    const cfg = loadConfig(argValue(rest, "--config"));
    const port = argValue(rest, "--port") ?? String(cfg.http.port);
    const token =
      argValue(rest, "--token") ??
      process.env.CAR_INGEST_TOKEN ??
      (cfg.http.ingest_token_envs.generic ? process.env[cfg.http.ingest_token_envs.generic] : undefined) ??
      (cfg.http.ingest_token_envs["*"] ? process.env[cfg.http.ingest_token_envs["*"]] : undefined) ??
      cfg.http.ingest_tokens.generic ??
      cfg.http.ingest_tokens["*"];
    if (!token) {
      console.error("card emit: no generic ingest credential; configure http.ingest_token_envs or CAR_INGEST_TOKEN");
      process.exit(1);
    }
    const res = await fetch(`http://127.0.0.1:${port}/v1/events`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
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
          pending_effects: q("SELECT COUNT(*) n FROM effects WHERE state IN ('proposed','pending','running')"),
          blocked_effects: q("SELECT COUNT(*) n FROM effects WHERE state = 'blocked'"),
          provider_invocations_running: q("SELECT COUNT(*) n FROM provider_invocations WHERE state = 'running'"),
          uncertain_delivery: q("SELECT COUNT(*) n FROM outbox WHERE state = 'uncertain'"),
          open_escalations: q("SELECT COUNT(*) n FROM escalations WHERE state = 'pending'"),
          active_sessions: q("SELECT COUNT(*) n FROM sessions WHERE state = 'active'"),
          panic: store.getPanicState(),
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
      validateProviderTopology(cfg);
      checks.push(["provider topology", true, `${Object.keys(cfg.providers.instances).length} configured instance(s)`]);
    } catch (err) {
      checks.push(["provider topology", false, String(err)]);
    }
    try {
      openStore(dbPath(cfg)).db.close();
      checks.push(["sqlite", true, dbPath(cfg)]);
    } catch (err) {
      checks.push(["sqlite", false, String(err)]);
    }
    checks.push([
      "telegram",
      !cfg.telegram.enabled ||
        (Boolean(process.env[cfg.telegram.token_env]) &&
          Boolean(cfg.telegram.chat_id) &&
          cfg.telegram.allowed_user_ids.length > 0),
      cfg.telegram.enabled
        ? `token via $${cfg.telegram.token_env}; chat ${cfg.telegram.chat_id || "MISSING"}; allowed users ${cfg.telegram.allowed_user_ids.length}`
        : "disabled",
    ]);
    const configuredIngestCredentials = new Set([
      ...Object.values(cfg.http.ingest_tokens),
      ...Object.values(cfg.http.ingest_token_envs)
        .map((name) => process.env[name])
        .filter((value): value is string => Boolean(value)),
    ]);
    checks.push([
      "write authentication",
      configuredIngestCredentials.size > 0,
      configuredIngestCredentials.size > 0
        ? `${configuredIngestCredentials.size} credential(s) available`
        : "no write caller can authenticate; configure http.ingest_token_envs",
    ]);
    const hermesInstances = Object.entries(cfg.providers.instances).filter(([, instance]) => instance.adapter === "hermes");
    for (const [name, instance] of hermesInstances) {
      const executable = instance.executable ?? "hermes";
      const argv = [executable, "-p", instance.profile!, "acp", "--check"];
      try {
        const result = Bun.spawnSync(argv, { stdout: "pipe", stderr: "pipe" });
        const detail = new TextDecoder().decode(result.stdout).trim() || new TextDecoder().decode(result.stderr).trim();
        checks.push([`Hermes ${name}`, result.exitCode === 0, detail || `${argv.join(" ")} exited ${result.exitCode}`]);
      } catch (err) {
        checks.push([`Hermes ${name}`, false, String(err)]);
      }
    }
    checks.push([
      "dead-man observer",
      !cfg.deadman.enabled || Boolean(process.env[cfg.deadman.token_env]),
      cfg.deadman.enabled ? `credential via $${cfg.deadman.token_env}` : "disabled",
    ]);
    for (const [name, ok, detail] of checks) console.log(`${ok ? "ok " : "FAIL"} ${name}: ${detail}`);
    process.exit(checks.every(([, ok]) => ok) ? 0 : 1);
  }


  default:
    console.log(ATTENTION_HELP + "\nAdvanced: card emit | status | doctor");
    process.exit(cmd && cmd !== "--help" && cmd !== "help" ? 1 : 0);
}
