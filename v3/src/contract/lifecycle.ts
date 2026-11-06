/**
 * Core lifecycle contracts shared by repositories, providers, and surfaces.
 * This module is wire-only: it must not import from the store, router, or a
 * provider implementation.
 */
import { z } from "zod";

export const EffectType = z.enum([
  "escalate",
  "notify",
  "defer",
  "reply",
  "approve",
  "deny",
  "probe",
  "run_template",
]);
export type EffectType = z.infer<typeof EffectType>;

export const EffectState = z.enum([
  "proposed",
  "blocked",
  "pending",
  "running",
  "terminal_recorded",
]);
export type EffectState = z.infer<typeof EffectState>;

export const EffectTerminalOutcome = z.enum(["ok", "failed", "cancelled", "expired", "uncertain"]);
export type EffectTerminalOutcome = z.infer<typeof EffectTerminalOutcome>;

export const ProviderInvocationState = z.enum(["pending", "running", "terminal_recorded"]);
export type ProviderInvocationState = z.infer<typeof ProviderInvocationState>;

export const ProviderTerminalOutcome = z.enum([
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
  "unknown",
]);
export type ProviderTerminalOutcome = z.infer<typeof ProviderTerminalOutcome>;

/**
 * Public agentctl execution states used by the compact Runs projection.
 * Keep one normalized vocabulary so observation, persistence, and UI cannot
 * disagree about whether a run is terminal or successful.
 */
export const AGENT_RUN_SUCCESS_STATES = ["completed", "succeeded", "ok"] as const;
export const AGENT_RUN_FAILURE_STATES = [
  "failed",
  "cancelled",
  "canceled",
  "timed_out",
  "orphaned",
  "error",
] as const;
export const AGENT_RUN_TERMINAL_STATES = [
  ...AGENT_RUN_SUCCESS_STATES,
  ...AGENT_RUN_FAILURE_STATES,
] as const;

const agentRunSuccessStates = new Set<string>(AGENT_RUN_SUCCESS_STATES);
const agentRunFailureStates = new Set<string>(AGENT_RUN_FAILURE_STATES);
const agentRunTerminalStates = new Set<string>(AGENT_RUN_TERMINAL_STATES);

export function normalizeAgentRunState(state: string): string {
  return state.trim().toLowerCase();
}

export function isAgentRunSuccessState(state: string): boolean {
  return agentRunSuccessStates.has(normalizeAgentRunState(state));
}

export function isAgentRunFailureState(state: string): boolean {
  return agentRunFailureStates.has(normalizeAgentRunState(state));
}

export function isAgentRunTerminalState(state: string): boolean {
  return agentRunTerminalStates.has(normalizeAgentRunState(state));
}

export const ProviderRecoveryState = z.enum([
  "lease_expired",
  "reclaimed",
  "resume_requested",
  "abandoned",
]);
export type ProviderRecoveryState = z.infer<typeof ProviderRecoveryState>;

export const HumanFactKind = z.enum([
  "feedback",
  "instruction",
  "reply",
  "grant_created",
  "grant_revoked",
  "decision_outcome",
]);
export type HumanFactKind = z.infer<typeof HumanFactKind>;

export const InteractionState = z.enum([
  "received",
  "acknowledged",
  "consumed",
  "rejected",
  "expired",
]);
export type InteractionState = z.infer<typeof InteractionState>;

export const GrantStatus = z.enum(["active", "consumed", "rejected", "revoked", "expired"]);
export type GrantStatus = z.infer<typeof GrantStatus>;

export const OutboxState = z.enum([
  "pending",
  "sending",
  "delivered",
  "uncertain",
  "failed",
  "deferred",
  "expired",
  "superseded",
  "abandoned",
  "suppressed",
  "no_target",
  // v2 compatibility states remain readable while the migration bridge exists.
  "sent",
  "dead",
]);
export type OutboxState = z.infer<typeof OutboxState>;

export const ProviderEventType = z.enum([
  "started",
  "heartbeat",
  "progress_delta",
  "progress_snapshot",
  "artifact",
  "final_answer",
  "terminal_result",
  "recovery_state",
]);
export type ProviderEventType = z.infer<typeof ProviderEventType>;

export const ProviderEvent = z.object({
  contract: z.literal("car.provider-event.v1"),
  invocation_id: z.string().min(1),
  request_id: z.string().min(1),
  provider_id: z.string().min(1),
  provider_instance: z.string().min(1),
  type: ProviderEventType,
  seq: z.number().int().nonnegative(),
  ts: z.iso.datetime({ offset: true }),
  payload: z.record(z.string(), z.unknown()).default({}),
});
export type ProviderEvent = z.infer<typeof ProviderEvent>;

export const EffectProposal = z.object({
  contract: z.literal("car.effect-proposal.v1"),
  intent_id: z.string().min(1),
  effect_type: EffectType,
  args: z.record(z.string(), z.unknown()).default({}),
  lineage_id: z.string().min(1),
  rationale: z.string().max(16_384).default(""),
});
export type EffectProposal = z.infer<typeof EffectProposal>;
