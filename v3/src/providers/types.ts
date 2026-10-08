/**
 * Public CAR capability-provider contracts.
 *
 * This module is deliberately transport and store agnostic.  A provider can
 * run in-process (the native provider) or behind ACP (Hermes), but the router
 * sees the same closed, versioned messages in either case.
 */
import { z } from "zod";
import { EffectType as EffectTypeSchema, type EffectProposal, type EffectType } from "../contract/lifecycle.ts";
import type { ResolvedProvider } from "../config/provider_topology.ts";

export const PROVIDER_PROTOCOL_VERSION = "car.provider.v1" as const;
export const OPERATOR_CONTRACT_VERSION = "car.operator.v1" as const;
export const POLICY_CONTRACT_VERSION = "car.policy.v1" as const;
export const MEMORY_CONTRACT_VERSION = "car.memory.v1" as const;

export const PROVIDER_CAPABILITIES = ["operator", "policy", "memory"] as const;
export type ProviderCapability = (typeof PROVIDER_CAPABILITIES)[number];

export const ProviderCapability = z.enum(PROVIDER_CAPABILITIES);
export const CapabilityContractVersion = z.object({
  operator: z.literal(OPERATOR_CONTRACT_VERSION).optional(),
  policy: z.literal(POLICY_CONTRACT_VERSION).optional(),
  memory: z.literal(MEMORY_CONTRACT_VERSION).optional(),
}).strict();

export const ProviderDescriptor = z.object({
  contract: z.literal(PROVIDER_PROTOCOL_VERSION),
  provider_id: z.string().min(1).max(128),
  provider_version: z.string().min(1).max(128),
  capabilities: z.array(ProviderCapability).min(1),
  contracts: CapabilityContractVersion,
  provider_instance: z.string().min(1).max(256),
  profile: z.string().min(1).max(256).optional(),
  continuity: z.enum(["global", "scoped", "incident"]),
  continuity_key: z.string().min(1).max(256),
  state_root: z.string().min(1).max(4096),
  config_fingerprint: z.string().min(1).max(128),
}).strict().superRefine((value, ctx) => {
  if (new Set(value.capabilities).size !== value.capabilities.length) {
    ctx.addIssue({ code: "custom", path: ["capabilities"], message: "capabilities must not be repeated" });
  }
});
export type ProviderDescriptor = z.infer<typeof ProviderDescriptor>;

export const ProviderFailureCode = z.enum([
  "version_mismatch",
  "unsupported_capability",
  "duplicate_provider",
  "invalid_request",
  "invalid_response",
  "not_ready",
  "deadline_exceeded",
  "cancelled",
  "transport_unavailable",
  "transport_protocol",
  "state_unavailable",
  "unknown",
]);
export type ProviderFailureCode = z.infer<typeof ProviderFailureCode>;

export const ProviderFailure = z.object({
  contract: z.literal("car.provider-failure.v1"),
  code: ProviderFailureCode,
  message: z.string().min(1).max(4096),
  retryable: z.boolean(),
  provider_id: z.string().min(1),
  provider_instance: z.string().min(1),
  request_id: z.string().min(1),
  details: z.record(z.string(), z.unknown()).default({}),
}).strict();
export type ProviderFailure = z.infer<typeof ProviderFailure>;

export const ProviderPreflight = z.object({
  ready: z.boolean(),
  checked_at: z.iso.datetime({ offset: true }),
  diagnostics: z.array(z.object({
    code: z.string().min(1),
    message: z.string().min(1),
    severity: z.enum(["info", "warning", "error"]),
  })).default([]),
}).strict();
export type ProviderPreflight = z.infer<typeof ProviderPreflight>;

export const ProviderHealth = z.object({
  healthy: z.boolean(),
  checked_at: z.iso.datetime({ offset: true }),
  semantic_progress_at: z.iso.datetime({ offset: true }).nullable().optional(),
  details: z.record(z.string(), z.unknown()).default({}),
}).strict();
export type ProviderHealth = z.infer<typeof ProviderHealth>;

export interface ProviderRequestBase {
  request_id: string;
  deadline_at: string;
  provider: ProviderDescriptor;
}

export interface IncidentEventSnapshot {
  id: string;
  type: string;
  severity: string;
  title: string;
  body?: string;
  requires_response?: boolean;
  expires_at?: string | null;
  payload?: Record<string, unknown>;
}

export interface IncidentPacket extends ProviderRequestBase {
  contract: typeof OPERATOR_CONTRACT_VERSION;
  incident_id: string;
  car_session_id: string | null;
  dedupe_class?: string | null;
  events: IncidentEventSnapshot[];
  context: ContextBundle;
}

export const Disposition = z.enum(["resolve", "keep_informed", "escalate", "defer"]);
export type Disposition = z.infer<typeof Disposition>;

export interface ProviderDecision {
  contract: typeof OPERATOR_CONTRACT_VERSION;
  request_id: string;
  disposition: Disposition;
  rationale: string;
  effects: EffectProposal[];
  model?: string | null;
  usage?: { tokens_in: number; tokens_out: number; cost_usd: number };
}

export const OperatorDecision = z.object({
  contract: z.literal(OPERATOR_CONTRACT_VERSION),
  request_id: z.string().min(1),
  disposition: Disposition,
  rationale: z.string().max(16_384),
  effects: z.array(z.object({
    contract: z.literal("car.effect-proposal.v1"),
    intent_id: z.string().min(1),
    effect_type: EffectTypeSchema,
    args: z.record(z.string(), z.unknown()).default({}),
    lineage_id: z.string().min(1),
    rationale: z.string().max(16_384).default(""),
  })),
  model: z.string().nullable().optional(),
  usage: z.object({
    tokens_in: z.number().int().nonnegative(),
    tokens_out: z.number().int().nonnegative(),
    cost_usd: z.number().nonnegative(),
  }).optional(),
}).strict();

export interface EffectProposalPacket extends ProviderRequestBase {
  contract: typeof POLICY_CONTRACT_VERSION;
  effect: EffectProposal;
  incident_id: string | null;
  grant_active: boolean;
  grant_id?: string | null;
  verified_scope?: Record<string, string>;
}

export const PolicyVerdict = z.enum(["allow", "deny", "review"]);
export type PolicyVerdict = z.infer<typeof PolicyVerdict>;

export interface PolicyAdvice {
  contract: typeof POLICY_CONTRACT_VERSION;
  request_id: string;
  verdict: PolicyVerdict;
  rationale: string;
  constraints?: Record<string, unknown>;
}

export const PolicyAdvice = z.object({
  contract: z.literal(POLICY_CONTRACT_VERSION),
  request_id: z.string().min(1),
  verdict: PolicyVerdict,
  rationale: z.string().max(16_384),
  constraints: z.record(z.string(), z.unknown()).optional(),
}).strict();

export interface ContextQuery extends ProviderRequestBase {
  contract: typeof MEMORY_CONTRACT_VERSION;
  incident_id: string | null;
  vendor?: string;
  repo?: string;
  host?: string;
  event_type?: string;
  dedupe_class?: string;
  free_text?: string;
  token_budget: number;
}

export interface MemoryHit {
  id: string;
  tier: string;
  kind: string;
  content: Record<string, unknown>;
  confidence: number;
  autonomy: string;
  status: string;
}

export interface ContextBundle {
  charter: string;
  hits: MemoryHit[];
  provider_ref?: string;
}

export const ContextBundle = z.object({
  charter: z.string(),
  hits: z.array(z.object({
    id: z.string().min(1),
    tier: z.string().min(1),
    kind: z.string().min(1),
    content: z.record(z.string(), z.unknown()),
    confidence: z.number().min(0).max(1),
    autonomy: z.string(),
    status: z.string(),
  })),
  provider_ref: z.string().optional(),
}).strict();

export interface HumanOrDecisionOutcome extends ProviderRequestBase {
  contract: typeof MEMORY_CONTRACT_VERSION;
  kind: "feedback" | "instruction" | "reply" | "grant_created" | "grant_revoked" | "decision_outcome";
  fact_id: string;
  body: Record<string, unknown>;
  incident_id?: string | null;
}

/** A provider's public event. Private transcripts/files are never accepted as completion. */
export interface ProviderEventEnvelope {
  contract: "car.provider-event.v1";
  invocation_id: string;
  request_id: string;
  provider_id: string;
  provider_instance: string;
  type: "started" | "heartbeat" | "progress_delta" | "progress_snapshot" | "artifact" | "final_answer" | "terminal_result" | "recovery_state";
  seq: number;
  ts: string;
  payload: Record<string, unknown>;
}

/** The adapter boundary used by the registry. */
export interface CapabilityProvider {
  readonly descriptor: ProviderDescriptor;
  preflight(): Promise<ProviderPreflight>;
  health(): Promise<ProviderHealth>;
  decide?(input: IncidentPacket): Promise<ProviderDecision>;
  evaluate?(input: EffectProposalPacket): Promise<PolicyAdvice>;
  context?(input: ContextQuery): Promise<ContextBundle>;
  observe?(input: HumanOrDecisionOutcome): Promise<void>;
  close?(): Promise<void>;
}

/** Narrow capability views used by the router when selecting one slot. */
export type OperatorProvider = CapabilityProvider & Required<Pick<CapabilityProvider, "decide">>;
export type PolicyProvider = CapabilityProvider & Required<Pick<CapabilityProvider, "evaluate">>;
export type MemoryProvider = CapabilityProvider & Required<Pick<CapabilityProvider, "context" | "observe">>;

const ProviderRequestBaseSchema = z.object({
  request_id: z.string().min(1).max(512),
  deadline_at: z.iso.datetime({ offset: true }),
  provider: ProviderDescriptor,
}).strict();

export const IncidentPacketSchema = ProviderRequestBaseSchema.extend({
  contract: z.literal(OPERATOR_CONTRACT_VERSION),
  incident_id: z.string().min(1),
  car_session_id: z.string().nullable(),
  dedupe_class: z.string().nullable().optional(),
  events: z.array(z.object({
    id: z.string().min(1),
    type: z.string().min(1),
    severity: z.string().min(1),
    title: z.string(),
    body: z.string().optional(),
    requires_response: z.boolean().optional(),
    expires_at: z.string().nullable().optional(),
    payload: z.record(z.string(), z.unknown()).optional(),
  }).strict()),
  context: z.object({
    charter: z.string(),
    hits: z.array(z.unknown()),
    provider_ref: z.string().optional(),
  }).strict(),
}).strict();

export const EffectProposalPacketSchema = ProviderRequestBaseSchema.extend({
  contract: z.literal(POLICY_CONTRACT_VERSION),
  effect: z.object({
    contract: z.literal("car.effect-proposal.v1"),
    intent_id: z.string().min(1),
    effect_type: EffectTypeSchema,
    args: z.record(z.string(), z.unknown()).default({}),
    lineage_id: z.string().min(1),
    rationale: z.string().max(16_384).default(""),
  }).strict(),
  incident_id: z.string().nullable(),
  grant_active: z.boolean(),
  grant_id: z.string().nullable().optional(),
  verified_scope: z.record(z.string(), z.string()).optional(),
}).strict();

export const ContextQuerySchema = ProviderRequestBaseSchema.extend({
  contract: z.literal(MEMORY_CONTRACT_VERSION),
  incident_id: z.string().nullable(),
  vendor: z.string().optional(),
  repo: z.string().optional(),
  host: z.string().optional(),
  event_type: z.string().optional(),
  dedupe_class: z.string().optional(),
  free_text: z.string().optional(),
  token_budget: z.number().int().nonnegative().max(100_000),
}).strict();

export const HumanOrDecisionOutcomeSchema = ProviderRequestBaseSchema.extend({
  contract: z.literal(MEMORY_CONTRACT_VERSION),
  kind: z.enum(["feedback", "instruction", "reply", "grant_created", "grant_revoked", "decision_outcome"]),
  fact_id: z.string().min(1),
  body: z.record(z.string(), z.unknown()),
  incident_id: z.string().nullable().optional(),
}).strict();

export function descriptorFromResolved(resolved: ResolvedProvider, providerVersion: string, capabilities: ProviderCapability[]): ProviderDescriptor {
  const contracts: Partial<Record<ProviderCapability, string>> = {};
  for (const capability of capabilities) {
    contracts[capability] = capability === "operator"
      ? OPERATOR_CONTRACT_VERSION
      : capability === "policy"
        ? POLICY_CONTRACT_VERSION
        : MEMORY_CONTRACT_VERSION;
  }
  return ProviderDescriptor.parse({
    contract: PROVIDER_PROTOCOL_VERSION,
    provider_id: resolved.provider_id,
    provider_version: providerVersion,
    capabilities,
    contracts,
    provider_instance: resolved.instance_id,
    ...(resolved.profile ? { profile: resolved.profile } : {}),
    continuity: resolved.continuity,
    continuity_key: resolved.continuity_key,
    state_root: resolved.state_root,
    config_fingerprint: resolved.config_fingerprint,
  });
}
