/**
 * Read-only agentctl observer.
 *
 * agentctl remains execution authority. CAR polls compact metadata via
 * `recent`; it never calls `result` (which acknowledges/mutates agentctl
 * reconciliation state) and never mirrors the high-volume progress journal.
 */
import type { CarConfig } from "../config/config.ts";
import { isAgentRunTerminalState } from "../contract/lifecycle.ts";
import type { Loop } from "../ports.ts";
import type { Store } from "../store/db.ts";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface AgentctlExecution {
  id: string;
  labels?: string[];
  authority?: string;
  adapter?: string;
  mode?: string;
  state: string;
  liveness?: string;
  created_at?: string;
  started_at?: string;
  updated_at: string;
  terminal_at?: string;
  duration_seconds?: number;
  revision?: number;
  unreconciled?: boolean;
  cwd?: string;
  repo?: string;
  profile?: string;
  model?: string;
  runtime?: string;
}

export interface AgentctlRecentPage {
  executions: AgentctlExecution[];
  hasMore: boolean;
}

export type AgentctlRecent = (filter: "nonterminal" | "recent") => Promise<AgentctlRecentPage>;

interface ObserverHealth {
  state: "ok" | "degraded" | "disabled";
  observed_at: string;
  error?: string;
  run_count?: number;
  coverage_degraded?: boolean;
  history_truncated?: boolean;
}

const HEALTH_KEY = "agentctl.observer.health";
const PRUNE_INTERVAL_MS = 60 * 60_000;

export function createAgentctlObserverLoop(
  store: Store,
  config: CarConfig,
  recent: AgentctlRecent = createRecentRunner(config),
): Loop {
  let timer: ReturnType<typeof setInterval> | null = null;
  let running: Promise<void> | null = null;
  let lastPrunedAt = 0;

  const recordHealth = (next: ObserverHealth) => {
    const previous = store.kvGet<ObserverHealth>(HEALTH_KEY);
    store.kvSet(HEALTH_KEY, next);
    if (!previous || previous.state !== next.state || previous.error !== next.error ||
      previous.coverage_degraded !== next.coverage_degraded ||
      previous.history_truncated !== next.history_truncated) {
      store.audit("daemon", `agentctl.observer_${next.state}`, "observer", "agentctl", next);
    }
  };

  const tick = async () => {
    if (!config.agentctl_observer.enabled || running) return running ?? Promise.resolve();
    running = (async () => {
      try {
        if (!config.agentctl_observer.observe_all && config.agentctl_observer.required_labels.length === 0) {
          throw new Error("agentctl observer requires exact labels or observe_all=true");
        }
        const [active, recentRuns] = await Promise.all([
          recent("nonterminal"),
          recent("recent"),
        ]);
        const rows = new Map<string, AgentctlExecution>();
        for (const run of active.executions) mergeExecution(rows, run);
        for (const run of recentRuns.executions) mergeExecution(rows, run);
        const invalidRows: string[] = [];
        for (const run of rows.values()) {
          try {
            observeRun(store, run);
          } catch (error) {
            invalidRows.push(`${run.id || "missing-id"}: ${conciseError(error)}`);
          }
        }
        if (invalidRows.length) {
          throw new Error(`agentctl recent returned ${invalidRows.length} invalid execution(s): ${invalidRows.slice(0, 3).join("; ")}`);
        }
        if (!active.hasMore) {
          const activeEvidence = new Set(active.executions.map((run) => run.id));
          // The two reads are independent snapshots. A run may be created
          // between them, so a nonterminal row in the unfiltered page is also
          // positive evidence and must not be flipped unknown this tick.
          for (const run of recentRuns.executions) {
            if (!isAgentRunTerminalState(run.state)) activeEvidence.add(run.id);
          }
          store.markMissingAgentRunsUnknown([...activeEvidence]);
        }
        const nowMs = store.clock.now().getTime();
        if (nowMs - lastPrunedAt >= PRUNE_INTERVAL_MS) {
          store.pruneAgentRuns(
            config.agentctl_observer.retention_days,
            config.agentctl_observer.retention_max_terminal,
          );
          lastPrunedAt = nowMs;
        }
        recordHealth({
          state: "ok",
          observed_at: store.clock.now().toISOString(),
          run_count: rows.size,
          coverage_degraded: active.hasMore,
          history_truncated: recentRuns.hasMore,
        });
      } catch (error) {
        recordHealth({
          state: "degraded",
          observed_at: store.clock.now().toISOString(),
          error: conciseError(error),
        });
      } finally {
        running = null;
      }
    })();
    return running;
  };

  return {
    name: "agentctl-observer",
    start() {
      if (!config.agentctl_observer.enabled) {
        recordHealth({ state: "disabled", observed_at: store.clock.now().toISOString() });
        return;
      }
      void tick();
      timer = setInterval(() => void tick(), config.agentctl_observer.interval_seconds * 1_000);
    },
    async stop() {
      if (timer) clearInterval(timer);
      timer = null;
      await running;
    },
  };
}

export function observeRun(store: Store, run: AgentctlExecution): void {
  if (!run.id || !run.state || !run.updated_at) throw new Error("agentctl recent returned an incomplete execution");
  const adapter = cleanAgent(run.adapter);
  const terminal = isAgentRunTerminalState(run.state);
  const stale = !terminal && run.liveness === "unreachable";
  store.upsertAgentRun({
    executionId: run.id,
    agent: adapter,
    authority: run.authority ?? null,
    mode: run.mode ?? null,
    state: run.state,
    liveness: run.liveness ?? null,
    labels: Array.isArray(run.labels) ? run.labels.filter((label): label is string => typeof label === "string") : [],
    title: runTitle(run, adapter),
    repo: run.repo ?? null,
    cwd: run.cwd ?? null,
    profile: run.profile ?? null,
    model: run.model ?? null,
    runtime: run.runtime ?? null,
    // A polled execution id is an agentctl transport id, not a native resume
    // id. Do not claim continuation capability from the adapter name alone.
    continuationSupported: false,
    startedAt: run.started_at ?? run.created_at ?? null,
    updatedAt: run.updated_at,
    terminalAt: run.terminal_at ?? null,
    durationSeconds: typeof run.duration_seconds === "number" ? run.duration_seconds : null,
    statusRevision: typeof run.revision === "number" ? run.revision : undefined,
    observationState: stale ? "stale" : "observed",
    lastMeaningfulUpdate: run.terminal_at ?? run.updated_at,
    raw: run as unknown as Record<string, unknown>,
  });
}

export function createRecentRunner(config: CarConfig): AgentctlRecent {
  return async (filter) => {
    const observer = config.agentctl_observer;
    const argv = [resolveAgentctlExecutable(observer.executable), "recent", "--limit", String(observer.discovery_limit)];
    if (filter === "nonterminal") argv.push("--state", "nonterminal");
    if (!observer.observe_all) {
      for (const label of observer.required_labels) argv.push("--label", label);
    }
    let result = await runRecent(argv, observer.command_timeout_seconds);
    if (result.code !== 0 && /journal_busy/i.test(result.stderr)) {
      await new Promise((resolve) => setTimeout(resolve, 125));
      result = await runRecent(argv, observer.command_timeout_seconds);
    }
    if (result.timedOut) throw new Error(`agentctl recent timed out after ${observer.command_timeout_seconds}s`);
    if (result.code !== 0) throw new Error(`agentctl recent exited ${result.code}: ${result.stderr.slice(0, 500)}`);
    if (result.stdout.length > 2_000_000) throw new Error("agentctl recent response exceeded 2 MB");
    return parseRecent(result.stdout);
  };
}

async function runRecent(
  argv: string[],
  timeoutSeconds: number,
): Promise<{ code: number; stdout: string; stderr: string; timedOut: boolean }> {
  const proc = Bun.spawn(argv, { stdout: "pipe", stderr: "pipe" });
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    proc.kill();
  }, timeoutSeconds * 1_000);
  try {
    const [code, stdout, stderr] = await Promise.all([
      proc.exited,
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    return { code, stdout, stderr, timedOut };
  } finally {
    clearTimeout(timeout);
  }
}

export function resolveAgentctlExecutable(configured: string): string {
  if (configured !== "agentctl" || configured.includes("/")) return configured;
  const fromEnv = process.env.AGENTCTL_BIN?.trim();
  if (fromEnv) return fromEnv;
  const fromPath = Bun.which("agentctl");
  if (fromPath) return fromPath;
  const managed = join(homedir(), ".local", "bin", "agentctl");
  return existsSync(managed) ? managed : configured;
}

export function parseRecent(stdout: string): AgentctlRecentPage {
  const parsed = JSON.parse(stdout) as { ok?: boolean; result?: { executions?: unknown; has_more?: unknown } };
  if (parsed.ok !== true || !Array.isArray(parsed.result?.executions) || typeof parsed.result.has_more !== "boolean") {
    throw new Error("agentctl recent returned an invalid envelope");
  }
  return {
    executions: parsed.result.executions as AgentctlExecution[],
    hasMore: parsed.result.has_more,
  };
}

function cleanAgent(adapter: string | undefined): string {
  const value = adapter?.trim().toLowerCase();
  return value || "agentctl";
}

function runTitle(run: AgentctlExecution, adapter: string): string {
  const labels = Array.isArray(run.labels) ? run.labels : [];
  const explicit = labels.find((label) => /^title:/i.test(label));
  if (explicit) return humanizeLabel(explicit.replace(/^title:/i, ""));
  const descriptive = labels.find((label) => {
    const normalized = label.trim().toLowerCase();
    return normalized.length > 0 && normalized !== adapter &&
      normalized !== "car-observe" && normalized !== "car-continuation" &&
      !normalized.startsWith("dogfood-") && !normalized.startsWith("car-");
  });
  return descriptive ? humanizeLabel(descriptive) : `${displayAgent(adapter)} run`;
}

function humanizeLabel(label: string): string {
  const value = label.trim().replace(/[-_]+/g, " ").replace(/\s+/g, " ");
  if (!value) return "Agent run";
  return value.replace(/\b\w/g, (part) => part.toUpperCase()).replace(/\bUi\b/g, "UI").replace(/\bOmp\b/g, "OMP");
}

function mergeExecution(rows: Map<string, AgentctlExecution>, run: AgentctlExecution): void {
  const existing = rows.get(run.id);
  if (!existing) {
    rows.set(run.id, run);
    return;
  }
  const revisionsComparable = typeof existing.revision === "number" && typeof run.revision === "number";
  const existingMs = Date.parse(existing.updated_at);
  const nextMs = Date.parse(run.updated_at);
  const timestampNewer = Number.isFinite(existingMs) && Number.isFinite(nextMs)
    ? nextMs > existingMs
    : run.updated_at > existing.updated_at;
  const sameTimestamp = Number.isFinite(existingMs) && Number.isFinite(nextMs)
    ? nextMs === existingMs
    : run.updated_at === existing.updated_at;
  const newer = revisionsComparable
    ? run.revision! > existing.revision! || (run.revision === existing.revision && timestampNewer)
    : timestampNewer || (sameTimestamp && isAgentRunTerminalState(run.state) && !isAgentRunTerminalState(existing.state));
  if (newer) rows.set(run.id, run);
}

function displayAgent(agent: string): string {
  if (agent === "omp") return "OMP";
  if (agent === "codex") return "Codex";
  if (agent === "cursor") return "Cursor";
  if (agent === "claude" || agent === "claude-code") return "Claude Code";
  return agent.replace(/(^|[-_])\w/g, (part) => part.replace(/[-_]/, " ").toUpperCase());
}

function conciseError(error: unknown): string {
  return String(error).replace(/\s+/g, " ").slice(0, 500);
}
