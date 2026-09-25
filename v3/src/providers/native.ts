import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { createHash, randomUUID } from "node:crypto";
import { EffectProposal as EffectProposalSchema } from "../contract/lifecycle.ts";
import { descriptorFromResolved } from "./types.ts";
import type {
  CapabilityProvider,
  ContextBundle,
  ContextQuery,
  EffectProposalPacket,
  HumanOrDecisionOutcome,
  IncidentPacket,
  MemoryHit,
  PolicyAdvice,
  ProviderDecision,
  ProviderDescriptor,
  ProviderHealth,
  ProviderPreflight,
} from "./types.ts";
import { ProviderError } from "./errors.ts";

export const NATIVE_PROVIDER_VERSION = "native-1.0.0";

function now(): string {
  return new Date().toISOString();
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex").slice(0, 24);
}

function defaultDescriptor(stateRoot: string, instance = "native"): ProviderDescriptor {
  return descriptorFromResolved({
    capability: "operator",
    provider_id: "native",
    configured_instance_id: instance,
    instance_id: instance,
    continuity: "global",
    continuity_key: "global",
    state_root: stateRoot,
    config_fingerprint: digest({ instance, stateRoot }),
    matched_route_index: null,
  }, NATIVE_PROVIDER_VERSION, ["operator", "policy", "memory"]);
}

interface LocalRecord {
  id: string;
  kind: string;
  content: Record<string, unknown>;
  scope: Record<string, string>;
  confidence: number;
  autonomy: "none" | "suggest" | "granted";
  status: "active" | "pending" | "archived";
  created_at: string;
  updated_at: string;
}

/** Small inspectable provider-owned memory; no embeddings, daemon, or API key. */
export class NativeMemoryProvider {
  readonly stateRoot: string;
  private readonly recordsPath: string;

  constructor(stateRoot: string) {
    this.stateRoot = stateRoot;
    this.recordsPath = join(stateRoot, "memory.json");
  }

  private read(): LocalRecord[] {
    if (!existsSync(this.recordsPath)) return [];
    try {
      const value = JSON.parse(readFileSync(this.recordsPath, "utf8")) as unknown;
      return Array.isArray(value) ? value.filter((row): row is LocalRecord => Boolean(row && typeof row === "object")) : [];
    } catch {
      throw new Error(`native memory state is unreadable: ${this.recordsPath}`);
    }
  }

  private write(rows: LocalRecord[]): void {
    mkdirSync(dirname(this.recordsPath), { recursive: true });
    const temporary = `${this.recordsPath}.${process.pid}.${randomUUID()}.tmp`;
    try {
      writeFileSync(temporary, `${JSON.stringify(rows, null, 2)}\n`, { flag: "wx", mode: 0o600 });
      renameSync(temporary, this.recordsPath);
    } finally {
      rmSync(temporary, { force: true });
    }
  }

  context(input: ContextQuery): ContextBundle {
    const rows = this.read();
    const query = (input.free_text ?? "").toLowerCase().split(/\s+/).filter(Boolean);
    const matches = rows
      .filter((row) => row.status !== "archived")
      .filter((row) => Object.entries(row.scope).every(([key, value]) => {
        const actual = (input as unknown as Record<string, unknown>)[key];
        return value === "*" || (typeof actual === "string" && (value.includes("*") ? new RegExp(`^${value.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`).test(actual) : value === actual));
      }))
      .filter((row) => query.length === 0 || query.some((term) => JSON.stringify(row.content).toLowerCase().includes(term)))
      .sort((a, b) => b.confidence - a.confidence || b.updated_at.localeCompare(a.updated_at))
      .slice(0, 20);
    const hits: MemoryHit[] = matches.map((row) => ({
      id: row.id,
      tier: row.kind === "rule" ? "rule" : "note",
      kind: row.kind,
      content: row.content,
      confidence: row.confidence,
      autonomy: row.autonomy,
      status: row.status,
    }));
    let charter = "";
    const charterPath = join(this.stateRoot, "charter.md");
    if (existsSync(charterPath)) charter = readFileSync(charterPath, "utf8");
    const budget = Math.max(0, Math.floor(input.token_budget));
    let used = Math.ceil(charter.length / 4);
    const bounded = hits.filter((hit) => {
      const cost = Math.ceil(JSON.stringify(hit.content).length / 4);
      if (used + cost > budget) return false;
      used += cost;
      return true;
    });
    return { charter, hits: bounded, provider_ref: `${this.stateRoot}/memory.json` };
  }

  observe(input: HumanOrDecisionOutcome): void {
    const rows = this.read();
    const id = `native_${digest({ instance: this.stateRoot, fact: input.fact_id })}`;
    // Observation delivery is at-least-once. Durable identity, not an
    // in-memory set, makes replay after a crash or provider restart harmless.
    if (rows.some((row) => row.id === id || row.content.fact_id === input.fact_id)) return;
    const stamp = now();
    rows.push({
      id,
      kind: input.kind,
      content: { ...input.body, fact_id: input.fact_id, incident_id: input.incident_id ?? null },
      scope: {},
      confidence: input.kind === "decision_outcome" ? 0.5 : 1,
      autonomy: "none",
      status: "active",
      created_at: stamp,
      updated_at: stamp,
    });
    this.write(rows);
  }
}

export interface NativeProviderOptions {
  descriptor?: ProviderDescriptor;
  stateRoot?: string;
  /** Inject a provider-owned memory implementation for tests or migration. */
  memory?: NativeMemoryProvider;
}

/** Deterministic, no-external-runtime reference implementation of all slots. */
export class NativeProvider implements CapabilityProvider {
  readonly descriptor: ProviderDescriptor;
  readonly memoryProvider: NativeMemoryProvider;
  private lastProgressAt: string | null = null;

  constructor(options: NativeProviderOptions = {}) {
    const stateRoot = options.stateRoot ?? join(homedir(), ".car", "providers", "native", "instances", "native", "global", "state");
    this.descriptor = options.descriptor ?? defaultDescriptor(stateRoot);
    this.memoryProvider = options.memory ?? new NativeMemoryProvider(this.descriptor.state_root);
  }

  async preflight(): Promise<ProviderPreflight> {
    return {
      ready: Boolean(this.descriptor.state_root),
      checked_at: now(),
      diagnostics: [{ code: "native.no_external_runtime", message: "native provider requires no external runtime, service, or API key", severity: "info" }],
    };
  }

  async health(): Promise<ProviderHealth> {
    return {
      healthy: true,
      checked_at: now(),
      semantic_progress_at: this.lastProgressAt,
      details: { deterministic: true, external_runtime: false },
    };
  }

  async decide(input: IncidentPacket): Promise<ProviderDecision> {
    this.ensureCapability("operator", input.request_id);
    if (input.events.length === 0) throw new ProviderError("invalid_request", "operator packet has no events", this.descriptor, input.request_id, { retryable: false });
    const urgent = input.events.some((event) => event.severity === "urgent");
    const errors = input.events.some((event) => event.type === "attention.error");
    const needsReply = input.events.some((event) => event.requires_response === true || event.type.startsWith("attention."));
    const trivial = input.events.every((event) => ["heartbeat", "progress", "session.started", "artifact", "cost.report"].includes(event.type));
    const disposition = urgent || errors || needsReply ? "escalate" : trivial ? "keep_informed" : "resolve";
    // Attention routing is expressed by disposition; effects are reserved for
    // typed external actuation proposals. This avoids asking for a human grant
    // merely to notify that a human grant is required.
    const effects: ReturnType<typeof EffectProposalSchema.parse>[] = [];
    this.lastProgressAt = now();
    return {
      contract: "car.operator.v1",
      request_id: input.request_id,
      disposition,
      rationale: urgent ? "urgent event" : errors ? "error event" : needsReply ? "event requires a response" : trivial ? "routine lifecycle event" : "no attention condition detected",
      effects,
      model: "native/deterministic",
      usage: { tokens_in: 0, tokens_out: 0, cost_usd: 0 },
    };
  }

  async evaluate(input: EffectProposalPacket): Promise<PolicyAdvice> {
    this.ensureCapability("policy", input.request_id);
    const risky = input.effect.effect_type === "approve" || input.effect.effect_type === "deny" || input.effect.effect_type === "run_template";
    const verdict = risky && !input.grant_active ? "review" : "allow";
    this.lastProgressAt = now();
    return {
      contract: "car.policy.v1",
      request_id: input.request_id,
      verdict,
      rationale: risky && !input.grant_active ? "mutating or permission effect requires an explicit core grant" : "native deterministic policy permits this proposal for core safety evaluation",
      constraints: risky ? { requires_core_grant: true } : {},
    };
  }

  async context(input: ContextQuery): Promise<ContextBundle> {
    this.ensureCapability("memory", input.request_id);
    let result: ContextBundle;
    try {
      result = this.memoryProvider.context(input);
    } catch (cause) {
      throw new ProviderError("state_unavailable", "native provider memory state is unavailable", this.descriptor, input.request_id, { retryable: true, cause });
    }
    this.lastProgressAt = now();
    return result;
  }

  async observe(input: HumanOrDecisionOutcome): Promise<void> {
    this.ensureCapability("memory", input.request_id);
    try {
      this.memoryProvider.observe(input);
    } catch (cause) {
      throw new ProviderError("state_unavailable", "native provider memory state is unavailable", this.descriptor, input.request_id, { retryable: true, cause });
    }
    this.lastProgressAt = now();
  }

  async close(): Promise<void> {
    // Native has no process or socket to own; state is flushed per operation.
  }

  private ensureCapability(capability: "operator" | "policy" | "memory", requestId: string): void {
    if (!this.descriptor.capabilities.includes(capability)) {
      throw new ProviderError("unsupported_capability", `native instance does not advertise ${capability}`, this.descriptor, requestId, { retryable: false });
    }
  }
}

export function createNativeProvider(options: NativeProviderOptions = {}): NativeProvider {
  return new NativeProvider(options);
}
