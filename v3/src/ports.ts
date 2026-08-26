/**
 * Cross-module interfaces (ports). FROZEN after scaffold: workstreams implement
 * these and code against each other's fakes; changes go through the coordinator.
 */
import type { Store } from "./store/db.ts";
import type { CarConfig } from "./config/config.ts";
import type { ResponseChannel, Severity } from "./contract/events.ts";

/* ------------------------------------------------------------------ triage */

export interface IncidentContext {
  incidentId: string;
  carSessionId: string | null;
  eventIds: string[];
}

/** Implemented by WS-B (src/triage). Called by the daemon's triage loop. */
export interface TriagePort {
  /** Claim + coalesce + triage one batch. Returns number of incidents processed. */
  tick(): Promise<number>;
}

/* ------------------------------------------------------------------ policy */

export type PolicyVerdict = "auto" | "escalate" | "forbid";

/** Implemented by WS-B (src/policy). Consulted by the action bus executor. */
export interface PolicyPort {
  /** Verdict for an action class + args; enforces class enablement, allowlists, guards. */
  check(actionClass: string, args: Record<string, unknown>): PolicyVerdict;
  /** Rate/dedupe/breaker/budget gates; returns null when clear, else a block reason. */
  gate(actionClass: string, dedupeHash: string): string | null;
  /** True when the circuit breaker or budget stop has flipped CAR to escalate-only. */
  escalateOnly(): boolean;
}

/* ----------------------------------------------------------------- actions */

export type DeliveryResult = "delivered" | "degraded" | "queued" | "failed";

export interface ReplyPayload {
  text?: string;
  approval?: boolean;
}

/** Implemented by WS-E (src/actions). Used by triage tools and Telegram buttons. */
export interface ActionBus {
  /** Route a reply/approval to a session via its response channel; falls back to file. */
  deliver(
    carSessionId: string,
    channel: ResponseChannel | null,
    payload: ReplyPayload,
  ): Promise<DeliveryResult>;
  /** Execute an allowlisted probe/action template. Executor enforces policy. */
  runTemplate(
    templateId: string,
    args: Record<string, unknown>,
    opts: { decisionId: string; mutating: boolean },
  ): Promise<{ ok: boolean; output: string }>;
}

/* ---------------------------------------------------------------- channels */

export interface EscalationMessage {
  escalationId: string;
  incidentId: string;
  carSessionId: string | null;
  severity: Severity;
  question: string;
  contextLines: string[];
  suggestedActionLabel?: string;
}

/** Implemented by WS-D (src/surfaces/telegram). All sends go through the outbox. */
export interface ChannelPort {
  /** Enqueue an escalation message (with buttons) for delivery. */
  sendEscalation(msg: EscalationMessage): void;
  /** Enqueue a plain notify line, optionally threaded to a session. */
  sendNotify(text: string, carSessionId?: string): void;
  /** Enqueue the daily digest. */
  sendDigest(markdown: string): void;
}

/* ------------------------------------------------------------------ memory */

export interface MemoryHit {
  id: string;
  tier: string;
  kind: string;
  content: Record<string, unknown>;
  confidence: number;
  autonomy: string;
  status: string;
}

/** Implemented by WS-C (src/memory). Read side used by triage prompt assembly. */
export interface MemoryReader {
  /** Deterministic pre-fetch for a triage prompt (charter text + scoped rules + notes + episodes). */
  assembleContext(input: {
    vendor?: string;
    repo?: string;
    eventType?: string;
    dedupeClass?: string;
    carSessionId?: string;
    freeText?: string;
    tokenBudget: number;
  }): { charter: string; hits: MemoryHit[] };
  search(query: string, scope?: Record<string, string>): MemoryHit[];
  get(id: string): MemoryHit | null;
  /** Active granted-autonomy rules matching an event, for the rules pass. */
  grantedRules(input: { vendor?: string; repo?: string; eventType?: string; dedupeClass?: string }): MemoryHit[];
}

/** Implemented by WS-C. Write side: David, triage proposals, outcome recorder. */
export interface MemoryWriter {
  addFromDavid(tier: string, kind: string, content: Record<string, unknown>, scope: Record<string, unknown>): string;
  propose(kind: string, content: Record<string, unknown>, scope: Record<string, unknown>): string;
  /** Deterministic outcome feedback: updates evidence counters, demotes on override. */
  recordOutcome(input: {
    decisionId: string;
    escalationId?: string;
    verdict: "confirmed" | "overridden" | "corrected" | "flagged";
    davidAction?: Record<string, unknown>;
  }): void;
  setAutonomy(memoryId: string, autonomy: "none" | "suggest" | "granted", by: "david"): void;
}

/* --------------------------------------------------------------------- llm */

export interface LlmToolCall {
  tool: string;
  args: Record<string, unknown>;
}

export interface LlmTurnResult {
  toolCalls: LlmToolCall[];
  tokensIn: number;
  tokensOut: number;
  costUsd: number;
  model: string;
}

/**
 * Thin seam over the AI SDK so triage is testable with a scripted fake.
 * WS-B implements the real one; test/fakes.ts provides the scripted one.
 */
export interface LlmRunner {
  turn(input: {
    system: string;
    messages: { role: "user" | "assistant" | "tool"; content: string }[];
    tools: { name: string; description: string; schema: Record<string, unknown> }[];
  }): Promise<LlmTurnResult>;
}

/* ------------------------------------------------------------------ daemon */

export interface Loop {
  name: string;
  start(): Promise<void> | void;
  stop(): Promise<void> | void;
}

export interface DaemonDeps {
  store: Store;
  config: CarConfig;
  triage: TriagePort;
  actions: ActionBus;
  channel: ChannelPort;
  memoryReader: MemoryReader;
  memoryWriter: MemoryWriter;
  policy: PolicyPort;
}
