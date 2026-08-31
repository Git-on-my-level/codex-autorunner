/**
 * Daemon configuration: ~/.car/config.toml (+ policy in policy.ts).
 * Defaults are fail-closed: localhost bind, no authenticated write callers,
 * no Telegram, and escalate-only until grants/safety allow effects.
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
      /** Bearer tokens per authenticated write-source id. Localhost is not exempt. */
      ingest_tokens: z.record(z.string(), z.string()).prefault({}),
      /** Optional env-var names containing tokens, keyed like ingest_tokens. */
      ingest_token_envs: z.record(z.string(), z.string()).prefault({}),
    })
    .prefault({}),
  telegram: z
    .object({
      enabled: z.boolean().default(false),
      token_env: z.string().default("CAR_TELEGRAM_TOKEN"),
      chat_id: z.string().default(""),
      /** Telegram user ids allowed to submit any inbound input. Chat ids are not identities. */
      allowed_user_ids: z
        .array(z.union([z.string().trim().min(1), z.number().int().nonnegative()]).transform(String))
        .default([]),
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
  safety: z
    .object({
      /** Immutable core rails; zero disables only the numeric cap, never grants or content checks. */
      max_effects_per_hour: z.number().int().nonnegative().default(60),
      max_failures_per_10m: z.number().int().nonnegative().default(5),
      /** Rolling 24-hour core effect cost budget. */
      max_effect_spend_usd: z.number().nonnegative().default(25),
      effect_lease_seconds: z.number().int().min(1).default(120),
      dedupe_minutes: z.number().int().nonnegative().default(30),
    })
    .prefault({}),
  deadman: z
    .object({
      enabled: z.boolean().default(false),
      url: z.string().url().default("https://example.invalid/car-deadman"),
      token_env: z.string().default("CAR_DEADMAN_TOKEN"),
      interval_seconds: z.number().int().min(10).default(60),
      timeout_seconds: z.number().int().min(1).max(30).default(10),
    })
    .prefault({}),
  agentctl_observer: z
    .object({
      /** Read-only reconciliation of local agentctl execution metadata. */
      enabled: z.boolean().default(false),
      executable: z.string().min(1).default("agentctl"),
      /** Exact labels to observe. Empty requires observe_all=true. */
      required_labels: z.array(z.string().trim().min(1)).default(["car-observe"]),
      observe_all: z.boolean().default(false),
      interval_seconds: z.number().int().min(5).default(15),
      /** agentctl recent rejects values above 200. */
      discovery_limit: z.number().int().min(1).max(200).default(100),
      command_timeout_seconds: z.number().int().min(1).max(60).default(10),
      /** Terminal rows are a rebuildable projection; unresolved runs are never pruned. */
      retention_days: z.number().int().min(1).max(3650).default(30),
      retention_max_terminal: z.number().int().min(100).max(100_000).default(2_000),
    })
    .refine((value) => !value.enabled || value.observe_all || value.required_labels.length > 0, {
      message: "enabled agentctl observer requires required_labels or observe_all=true",
    })
    .prefault({}),
  providers: z
    .object({
      defaults: z
        .object({
          operator: z.string().default("native"),
          policy: z.string().default("native"),
          memory: z.string().default("native"),
        })
        .prefault({}),
      routes: z
        .array(
          z.object({
            match: z.record(z.string(), z.string()).default({}),
            operator: z.string().optional(),
            policy: z.string().optional(),
            memory: z.string().optional(),
          }),
        )
        .default([]),
      instances: z
        .record(
          z.string(),
          z.object({
            adapter: z.enum(["native", "hermes"]),
            profile: z.string().optional(),
            continuity: z.enum(["global", "scoped", "incident"]).default("scoped"),
            scope: z.array(z.enum(["repo", "host", "source", "policy_domain"])).default(["repo"]),
            executable: z.string().optional(),
          }),
        )
        .prefault({ native: { adapter: "native", continuity: "global", scope: [] } }),
      /** Pre-ADR compatibility only; removed after native-provider cutover. */
      triage: z.string().default("anthropic/claude-haiku-4-5"),
      /** Pre-ADR compatibility only; removed after native-provider cutover. */
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
