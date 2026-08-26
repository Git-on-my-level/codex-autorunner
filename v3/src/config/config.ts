/**
 * Daemon configuration: ~/.car/config.toml (+ policy in policy.ts).
 * Defaults are safe: localhost bind, no Telegram until a token is set,
 * escalate-only until policy enables action classes.
 */
import { parse as parseToml } from "smol-toml";
import { z } from "zod";
import { homedir } from "node:os";
import { join } from "node:path";

export const CarConfig = z.object({
  state_dir: z.string().default(join(homedir(), ".car")),
  http: z
    .object({
      host: z.string().default("127.0.0.1"),
      port: z.number().int().default(7171),
      /** bearer tokens per source id for non-localhost ingest; localhost is trusted */
      ingest_tokens: z.record(z.string(), z.string()).prefault({}),
    })
    .prefault({}),
  telegram: z
    .object({
      enabled: z.boolean().default(false),
      token_env: z.string().default("CAR_TELEGRAM_TOKEN"),
      chat_id: z.string().default(""),
      /** forum supergroup with topic-per-session when true; flat chat otherwise */
      forum_mode: z.boolean().default(false),
      digest_time: z.string().default("08:30"), // local time HH:MM
    })
    .prefault({}),
  triage: z
    .object({
      coalesce_seconds: z.number().default(20),
      lease_seconds: z.number().default(120),
      max_tool_calls: z.number().default(8),
      max_llm_runs_per_incident: z.number().default(2),
      run_token_cap: z.number().default(16000),
    })
    .prefault({}),
  watchdog: z
    .object({
      pending_response_hours: z.number().default(4),
      default_heartbeat_multiple_warn: z.number().default(2),
      default_heartbeat_multiple_escalate: z.number().default(4),
    })
    .prefault({}),
  providers: z
    .object({
      triage: z.string().default("anthropic/claude-haiku-4-5"),
      consolidation: z.string().default("anthropic/claude-sonnet-5"),
    })
    .prefault({}),
});
export type CarConfig = z.infer<typeof CarConfig>;

export function loadConfig(path?: string): CarConfig {
  const configPath = path ?? join(homedir(), ".car", "config.toml");
  const file = Bun.file(configPath);
  // Bun.file(...).size is 0 for missing files; treat missing as defaults.
  try {
    const text = require("node:fs").readFileSync(configPath, "utf8") as string;
    return CarConfig.parse(parseToml(text));
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return CarConfig.parse({});
    throw err;
  }
}

export function dbPath(cfg: CarConfig): string {
  return join(cfg.state_dir, "car.db");
}
export function charterPath(cfg: CarConfig): string {
  return join(cfg.state_dir, "memory", "charter.md");
}
export function repliesDir(cfg: CarConfig): string {
  return join(cfg.state_dir, "replies");
}
export function policyPath(cfg: CarConfig): string {
  return join(cfg.state_dir, "policy.toml");
}
