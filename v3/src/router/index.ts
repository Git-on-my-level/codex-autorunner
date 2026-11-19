/**
 * Durable attention-router worker.
 *
 * The router is intentionally a small composition seam.  It owns neither a
 * provider implementation nor an effect executor: it claims core rows,
 * builds authenticated/core-derived packets, and hands typed proposals to the
 * provider and safety boundaries.  Every provider request has a durable row
 * and a terminal result before its value is used to create a decision, effect,
 * escalation, or delivery intent.
 */
import type { CarConfig } from "../config/config.ts";
import {
  resolveProvider,
  type ProviderSelectionContext,
  type ResolvedProvider,
} from "../config/provider_topology.ts";
import type { EffectType as WireEffectType } from "../contract/lifecycle.ts";
import { payloadSha256 } from "../contract/ids.ts";
import type {
  ContextBundle,
  ContextQuery,
  EffectProposalPacket,
  IncidentPacket,
  PolicyAdvice,
  ProviderDecision,
  ProviderFailure,
  ProviderDescriptor,
  CapabilityProvider,
} from "../providers/types.ts";
import {
  ContextBundle as ContextBundleSchema,
  EffectProposalPacketSchema,
  OperatorDecision,
  PolicyAdvice as PolicyAdviceSchema,
  descriptorFromResolved,
} from "../providers/types.ts";
import { invokeCapability } from "../providers/invocation.ts";
import { ProviderError, failureFromUnknown } from "../providers/errors.ts";
import { ProviderRegistry } from "../providers/registry.ts";
import { EffectExecutor, type EffectExecutionResponse } from "../effects/index.ts";
import { canonicalize, type EffectProposal as SafetyEffectProposal, type SafetyKernel } from "../safety/index.ts";
import { classifyEvent } from "../triage/rules.ts";
import { dedupeClassFor } from "../triage/dedupe.ts";
import { TriageRepo, type IncidentRow } from "../triage/repo.ts";
import { loadTemplates, policyClassFor } from "../actions/templates.ts";
import type { ChannelPort } from "../ports.ts";
import { StaleClaimError, type Claim, type EventRow, type Store } from "../store/db.ts";

const DEFAULT_LIMIT = 20;
const DEFAULT_LEASE_SECONDS = 120;
const DEFAULT_MEMORY_TOKENS = 2_500;

type Capability = "memory" | "operator" | "policy";

interface SessionCore {
  car_session_id: string;
  vendor: string;
  host: string;
  repo: string | null;
  repo_verified: number;
  title: string | null;
}

export interface RouterTickResult {
  claimed: number;
  processed: number;
  resolved: number;
  escalated: number;
  providerFailures: number;
  effects: number;
  blockedEffects: number;
  failed: number;
}

export interface RouterOptions {
  store: Store;
  config: CarConfig;
  registry: ProviderRegistry;
  /**
   * Resolve an unregistered runtime identity (scoped/incident continuity).
   * The callback may register and preflight the provider itself; when it
   * returns a provider, the router registers and health-checks it as a
   * convenience.  A provider with a different runtime identity is rejected.
   */
  ensureProvider?: (resolved: ResolvedProvider, capability: Capability) => Promise<CapabilityProvider | void> | CapabilityProvider | void;
  channel: ChannelPort;
  safety: SafetyKernel;
  /** Optional only for tests/composition roots that already own the repo. */
  triage?: TriageRepo;
  /** An executor made with the same safety kernel and core adapters. */
  effects?: EffectExecutor;
  owner?: string;
  limit?: number;
  leaseSeconds?: number;
  memoryTokenBudget?: number;
  /** Set false to make policy judgment an explicit no-policy deployment. */
  policyEnabled?: boolean;
  intervalMs?: number;
}

export interface RouterLoop {
  readonly name: "attention-router";
  tick(): Promise<RouterTickResult>;
  start(): Promise<void>;
  stop(): Promise<void>;
}

interface InvocationResult<T> {
  value: T;
  descriptor: ProviderDescriptor;
  resolved: ResolvedProvider;
  requestId: string;
}

interface ProviderFailureResult {
  failure: ProviderFailure;
  resolved?: ResolvedProvider;
}

function parsePayload(row: EventRow): Record<string, unknown> {
  try {
    const parsed = JSON.parse(row.payload_json) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

/**
 * Resolve a source clearance to one authenticated, still-live native ask.
 * Session/card identity is only correlation metadata: it is not safe as a
 * target because one native session can contain multiple asks.  The Multica
 * adapter forwards either the canonical CAR event id or the source-scoped
 * request idempotency key; generic/malformed payloads fail closed here too.
 */
function exactClearanceTarget(store: Store, row: EventRow): EventRow | null {
  if (!row.source_id) return null;
  const payload = parsePayload(row);
  const requestEventId = payload.request_event_id;
  const requestKey = payload.request_idempotency_key;
  if ((typeof requestEventId === "string") === (typeof requestKey === "string")) return null;
  const target = typeof requestEventId === "string"
    ? store.db.query("SELECT * FROM events WHERE id = ? AND source_id = ?").get(requestEventId, row.source_id) as EventRow | null
    : store.db.query("SELECT * FROM events WHERE idempotency_key = ? AND source_id = ?").get(requestKey as string, row.source_id) as EventRow | null;
  if (!target || target.requires_response !== 1) return null;
  if (["resolved", "cancelled", "expired"].includes(target.obligation_state ?? "")) return null;
  // A sweeper may not have visited an expired row yet.  A late native close
  // must never rewrite that missed outcome as a successful resolution.
  if (target.expires_at && Date.parse(target.expires_at) <= store.clock.now().getTime()) return null;
  return target;
}

function sessionFor(store: Store, row: EventRow): SessionCore | null {
  if (!row.car_session_id) return null;
  return (store.db.query(
    `SELECT car_session_id, vendor, host, repo, repo_verified, title
       FROM sessions WHERE car_session_id = ?`,
  ).get(row.car_session_id) as SessionCore | null) ?? null;
}

/** Only values persisted in authenticated event/session rows enter routing. */
function selectionContext(row: EventRow, session: SessionCore | null, incidentId?: string): ProviderSelectionContext {
  const context: ProviderSelectionContext = {
    source: row.source_vendor,
    host: session?.host ?? row.source_host,
    ...(incidentId ? { incident_id: incidentId } : {}),
  };
  // A repo is useful only when the core session actually persisted one.  In
  // particular, never derive it from cwd or from an event title/payload.
  if (session?.repo_verified === 1 && session.repo && session.repo.trim()) context.repo = session.repo;
  return context;
}

function sourceIdFor(row: EventRow): string {
  return row.source_id ?? [row.source_vendor, row.source_host, row.source_adapter].join("\u0000");
}

function requestIdFor(capability: Capability, row: EventRow, incidentId: string, resolved: ResolvedProvider, suffix = ""): string {
  const digest = payloadSha256({
    capability,
    event_id: row.id,
    incident_id: incidentId,
    provider_instance: resolved.instance_id,
    config_fingerprint: resolved.config_fingerprint,
    suffix,
  }).slice(0, 32);
  return `router_${capability}_${digest}`;
}

function deadlineFor(row: EventRow, nowMs: number, fallbackMs = 120_000): string {
  const payload = parsePayload(row);
  const hint = payload.deadline_at ?? payload.deadlineAt;
  // Provider invocation enforces `deadline_at` against the process wall clock
  // (Date.now()). Use the later of that and the Store clock so replay clocks
  // cannot manufacture an already-expired provider request, while explicit
  // event/payload deadlines remain authoritative.
  const fallback = Math.max(Date.now(), nowMs) + fallbackMs;
  const candidates = [fallback, row.expires_at ? Date.parse(row.expires_at) : NaN,
    typeof hint === "string" ? Date.parse(hint) : NaN].filter(Number.isFinite);
  return new Date(Math.min(...candidates)).toISOString();
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return JSON.stringify({ unavailable: true, type: typeof value });
  }
}

function failureOutcome(failure: ProviderFailure): "failed" | "timed_out" | "cancelled" | "unknown" {
  if (failure.code === "deadline_exceeded") return "timed_out";
  if (failure.code === "cancelled") return "cancelled";
  if (failure.code === "unknown") return "unknown";
  return "failed";
}

function syntheticDescriptor(resolved: ResolvedProvider, capability: Capability): ProviderDescriptor {
  return descriptorFromResolved(resolved, "unknown", [capability]);
}

function unresolvedDescriptor(capability: Capability): ProviderDescriptor {
  return {
    contract: "car.provider.v1",
    provider_id: "router",
    provider_version: "unresolved",
    capabilities: [capability],
    contracts: {
      [capability]: capability === "operator" ? "car.operator.v1" : capability === "policy" ? "car.policy.v1" : "car.memory.v1",
    },
    provider_instance: `unresolved:${capability}`,
    continuity: "global",
    continuity_key: "global",
    state_root: "unresolved",
    config_fingerprint: "unresolved",
  } as ProviderDescriptor;
}

function decodeResult<T>(responseRef: string | null, schema: { parse(value: unknown): T }): T | null {
  if (!responseRef) return null;
  try {
    const raw = JSON.parse(responseRef) as { value?: unknown };
    if (!("value" in raw)) return null;
    return schema.parse(raw.value);
  } catch {
    return null;
  }
}

function wireEffectToSafety(
  providerEffect: NonNullable<ProviderDecision["effects"]>[number],
  row: EventRow,
  session: SessionCore | null,
  incidentId: string,
  operatorRequestId: string,
  providerInstance: string,
  config: CarConfig,
  attributedCostUsd: number,
): SafetyEffectProposal {
  const scope = {
    provider_instance: providerInstance,
    vendor: row.source_vendor,
    host: session?.host ?? row.source_host,
    source_id: sourceIdFor(row),
    car_session_id: row.car_session_id ?? undefined,
    event_type: row.type,
    ...(session?.repo_verified === 1 && session.repo && session.repo.trim() ? { repo: session.repo, repo_verified: true } : {}),
  };
  const lineage = {
    source_id: sourceIdFor(row),
    request_id: operatorRequestId,
    event_id: row.id,
    ...(row.payload_sha256 ? { payload_sha256: row.payload_sha256 } : {}),
    ...(session?.repo_verified === 1 && session.repo && session.repo.trim() ? { verified_repo: session.repo } : {}),
  };
  // Provider intent ids are namespaced by the immutable event/request lineage;
  // a buggy provider reusing an id cannot overwrite another event's effect.
  const intent = `router_effect_${payloadSha256({
    provider_intent_id: providerEffect.intent_id,
    event_id: row.id,
    incident_id: incidentId,
    request_id: operatorRequestId,
  }).slice(0, 40)}`;
  return {
    intent_id: intent,
    type: providerEffect.effect_type,
    args: providerEffect.args,
    scope,
    lineage,
    deadline_at: row.expires_at ?? null,
    action_class: coreActionClass(providerEffect.effect_type, providerEffect.args, config),
    // Provider-reported operator usage is split once across all effects from
    // the decision. It is core-derived rather than provider-controlled effect
    // metadata, so multiple effects cannot multiply or erase the same charge.
    cost_usd: attributedCostUsd,
    provider_policy_verdict: null,
  };
}

/**
 * Conservatively round one operator decision's reported cost up to the nearest
 * micro-dollar, then partition those integer micro-dollars deterministically
 * across its effects. The parts sum to one charge (never N charges), and a
 * tiny provider value cannot disappear through division/rounding.
 */
export function attributedEffectCost(totalUsd: number | undefined, index: number, count: number): number {
  if (totalUsd === undefined || totalUsd <= 0 || count <= 0 || index < 0 || index >= count) return 0;
  if (!Number.isFinite(totalUsd)) throw new Error("operator usage cost must be finite");
  const totalMicros = Math.ceil(totalUsd * 1_000_000);
  if (!Number.isSafeInteger(totalMicros)) return totalUsd / count;
  const base = Math.floor(totalMicros / count);
  const remainder = totalMicros % count;
  return (base + (index < remainder ? 1 : 0)) / 1_000_000;
}

function coreActionClass(type: WireEffectType, args: Record<string, unknown>, config: CarConfig): string | undefined {
  if (type === "run_template") {
    const templateId = typeof args.template_id === "string" ? args.template_id : "";
    if (!templateId) return undefined;
    try {
      const spec = loadTemplates(config).templates[templateId];
      return spec ? policyClassFor(templateId, spec) : undefined;
    } catch {
      return undefined;
    }
  }
  if (type === "probe") return typeof args.vendor === "string" ? `probe.${args.vendor}` : undefined;
  return type;
}

function wireEffectFromSafety(effect: SafetyEffectProposal, requestId: string, provider: ProviderDescriptor, incidentId: string): EffectProposalPacket {
  return EffectProposalPacketSchema.parse({
    contract: "car.policy.v1",
    request_id: requestId,
    deadline_at: effect.deadline_at ?? new Date(Date.now() + 120_000).toISOString(),
    provider,
    incident_id: incidentId,
    grant_active: false,
    effect: {
      contract: "car.effect-proposal.v1",
      intent_id: effect.intent_id,
      effect_type: effect.type as WireEffectType,
      args: effect.args,
      lineage_id: canonicalize(effect.lineage),
      rationale: "router safety proposal",
    },
    verified_scope: Object.fromEntries(Object.entries(effect.scope).filter(([, value]) => typeof value === "string")) as Record<string, string>,
  });
}

export function createRouter(options: RouterOptions): RouterLoop {
  const {
    store,
    config,
    registry,
    channel,
    safety,
  } = options;
  const repo = options.triage ?? new TriageRepo(store);
  const effects = options.effects ?? new EffectExecutor({ kernel: safety, owner: `${options.owner ?? "router"}-effects` });
  if (effects.kernel !== safety) throw new Error("router effects executor must use the router safety kernel");
  const owner = options.owner ?? "attention-router";
  const limit = Math.max(1, Math.min(100, options.limit ?? DEFAULT_LIMIT));
  const leaseSeconds = Math.max(1, options.leaseSeconds ?? DEFAULT_LEASE_SECONDS);
  const memoryTokenBudget = Math.max(0, options.memoryTokenBudget ?? DEFAULT_MEMORY_TOKENS);
  const policyEnabled = options.policyEnabled ?? true;
  let timer: ReturnType<typeof setInterval> | undefined;
  let ticking: Promise<RouterTickResult> | null = null;
  let urgentTicking: Promise<RouterTickResult> | null = null;
  let urgentTimer: ReturnType<typeof setInterval> | undefined;
  let stopped = false;
  function assertClaim(row: EventRow, claim: Claim): void {
    if (!store.renewEventLease(row.id, claim, leaseSeconds)) throw new StaleClaimError("event", row.id);
  }

  async function providerFor(resolved: ResolvedProvider, capability: Capability): Promise<CapabilityProvider | null> {
    let provider = registry.get(resolved.instance_id);
    if (!provider && options.ensureProvider) {
      const ensured = await options.ensureProvider(resolved, capability);
      provider = registry.get(resolved.instance_id);
      if (!provider && ensured) {
        if (ensured.descriptor.provider_instance !== resolved.instance_id || ensured.descriptor.config_fingerprint !== resolved.config_fingerprint) {
          throw new ProviderError("invalid_request", "ensured provider identity does not match resolved runtime identity", ensured.descriptor, "provider-ensure", { retryable: false });
        }
        registry.register(ensured);
        provider = ensured;
      }
      if (provider && !registry.isReady(resolved.instance_id)) {
        await registry.preflight(resolved.instance_id);
        await registry.health(resolved.instance_id);
      }
    }
    if (provider && (provider.descriptor.provider_instance !== resolved.instance_id || provider.descriptor.config_fingerprint !== resolved.config_fingerprint)) {
      throw new ProviderError("invalid_request", `registered provider does not match resolved runtime identity for ${capability}`, provider.descriptor, "provider-ensure", { retryable: false });
    }
    return provider;
  }

  function incidentFor(row: EventRow): IncidentRow {
    const attached = row.incident_id ? repo.getIncident(row.incident_id) : null;
    if (attached) return attached;
    // Correlation is not request identity. Distinct native asks must not inherit
    // each other's answer, even when they share a title or idempotency prefix.
    const dedupeClass = row.requires_response === 1
      ? `request:${payloadSha256({ source: sourceIdFor(row), event: row.id }).slice(0, 32)}`
      : dedupeClassFor(row);
    const existing = repo.findLineageIncident(row.car_session_id, dedupeClass, sourceIdFor(row));
    if (existing) return existing.state === "open" ? existing : repo.reopenIncident(existing.id);
    return repo.openIncident({
      carSessionId: row.car_session_id,
      openedByEvent: row.id,
      dedupeClass,
      summary: row.title,
    });
  }

  function escalation(row: EventRow, incident: IncidentRow, rationale: string, question = row.title || row.body || `${row.type} needs attention`, severity = row.severity, detail: Record<string, unknown> = {}, suggestedAction: Record<string, unknown> | null = null, notify = true): string {
    const decisionId = repo.recordDecision({
      id: `dec_${payloadSha256({ escalation_event: row.id, incident: incident.id }).slice(0, 36)}`,
      incidentId: incident.id,
      decidedBy: "rules",
      disposition: "escalate",
      rationale,
      model: null,
    });
    const escalationId = repo.createEscalation({
      id: `esc_${payloadSha256({ event: row.id, incident: incident.id }).slice(0, 36)}`,
      originEventId: row.id,
      incidentId: incident.id,
      severity,
      question,
      suggestedAction,
    });
    if (notify) channel.sendEscalation({
      escalationId,
      incidentId: incident.id,
      carSessionId: incident.car_session_id,
      severity: severity as "info" | "notice" | "attention" | "urgent",
      question,
      contextLines: [rationale],
    });
    repo.setIncidentState(incident.id, "escalated", { summary: question });
    store.audit("router", "router.escalated", "incident", incident.id, {
      decision_id: decisionId,
      escalation_id: escalationId,
      ...detail,
    });
    return escalationId;
  }

  function complete(row: EventRow, claim: Claim, state: string, incidentId?: string): void {
    store.completeEventClaim(row.id, claim, state, incidentId);
  }

  function recordDecisionOutcome(
    decisionId: string,
    row: EventRow,
    incidentId: string,
    outcome: string,
    detail: Record<string, unknown>,
  ): void {
    store.recordInteraction({
      sourceId: "core:attention-router",
      idempotencyKey: `decision:${decisionId}:outcome`,
      kind: "decision_outcome",
      targetType: "decision",
      targetId: decisionId,
      actorId: "core:attention-router",
      body: { event_id: row.id, incident_id: incidentId, outcome, ...detail },
      lineageId: row.id,
    });
  }

  async function invokeDurably<T>(
    capability: Capability,
    row: EventRow,
    incidentId: string,
    resolved: ResolvedProvider,
    input: IncidentPacket | ContextQuery | EffectProposalPacket,
    schema: { parse(value: unknown): T },
  ): Promise<InvocationResult<T>> {
    const initialProvider = registry.get(resolved.instance_id);
    const requestId = input.request_id;
    const created = store.createProviderInvocation({
      incidentId,
      providerId: resolved.provider_id,
      providerInstance: resolved.instance_id,
      providerVersion: initialProvider?.descriptor.provider_version ?? "unknown",
      capability,
      requestId,
      // Deadlines are execution metadata and are refreshed on a retried
      // nonterminal invocation. They must not conflict with the immutable
      // semantic request identity used for durable result replay.
      requestHash: payloadSha256(Object.fromEntries(Object.entries(input).filter(([key]) => key !== "deadline_at"))),
    });
    const existing = store.getProviderInvocation(created.id);
    if (existing?.state === "terminal_recorded") {
      if (existing.terminal_outcome !== "succeeded") {
        throw new ProviderError("transport_protocol", "provider invocation already terminated without success", syntheticDescriptor(resolved, capability), requestId, { retryable: false });
      }
      const replay = decodeResult(existing.response_ref, schema);
      if (replay === null) {
        throw new ProviderError("invalid_response", "durable provider response is unavailable for replay", syntheticDescriptor(resolved, capability), requestId, { retryable: false });
      }
      return {
        value: replay,
        descriptor: descriptorFromResolved(resolved, existing.provider_version, [capability]),
        resolved,
        requestId,
      };
    }
    let provider: CapabilityProvider | null;
    try {
      provider = await providerFor(resolved, capability);
    } catch (error) {
      const failure = failureFromUnknown(error, initialProvider?.descriptor ?? syntheticDescriptor(resolved, capability), requestId);
      const claim = store.claimProviderInvocation(created.id, `${owner}:provider`, leaseSeconds);
      if (claim) store.recordProviderTerminal(created.id, { owner: `${owner}:provider`, token: claim.claim_token! }, failureOutcome(failure), { error: failure });
      store.audit("router", "router.provider_failure", "provider_invocation", created.id, { capability, failure });
      throw error;
    }
    if (!provider) {
      const descriptor = syntheticDescriptor(resolved, capability);
      const failure = new ProviderError("unsupported_capability", `provider instance ${resolved.instance_id} is not registered`, descriptor, requestId, { retryable: false });
      const missingClaim = store.claimProviderInvocation(created.id, `${owner}:provider`, leaseSeconds);
      if (missingClaim) store.recordProviderTerminal(created.id, { owner: `${owner}:provider`, token: missingClaim.claim_token! }, "failed", { error: failure.failure });
      store.audit("router", "router.provider_failure", "provider_invocation", created.id, { capability, failure: failure.failure });
      throw failure;
    }
    // The synthetic descriptor is used to construct a packet before registry
    // lookup. Once an implementation is present, bind the packet to that
    // exact registered descriptor; this keeps provider identity/version
    // provenance real while retaining deterministic request ids.
    const providerInput = { ...input, provider: provider.descriptor } as typeof input;
    let claimed = store.claimProviderInvocation(created.id, `${owner}:provider`, leaseSeconds);
    if (!claimed) {
      throw new ProviderError("not_ready", `provider invocation ${requestId} is currently owned by another worker`, provider.descriptor, requestId, { retryable: true });
    }
    const providerClaim = { owner: `${owner}:provider`, token: claimed.claim_token! };
    const renewTimer = setInterval(() => {
      if (!store.renewProviderInvocationLease(created.id, providerClaim, leaseSeconds)) {
        store.audit("router", "provider.lease_lost", "provider_invocation", created.id, {});
      }
    }, Math.max(100, Math.floor(leaseSeconds * 1_000 / 3)));
    try {
      const ready = registry.assertReady(resolved.instance_id, requestId);
      const result = await invokeCapability(ready, capability, providerInput);
      const responseRef = safeJson({ value: result.value });
      store.recordProviderTerminal(created.id, { owner: `${owner}:provider`, token: claimed.claim_token! }, "succeeded", { responseRef });
      store.audit("router", "router.provider_events", "provider_invocation", created.id, {
        capability,
        count: result.events.length,
        terminal: result.events.at(-1)?.type ?? null,
      });
      return { value: schema.parse(result.value), descriptor: ready.descriptor, resolved, requestId };
    } catch (error) {
      const failure = failureFromUnknown(error, provider.descriptor, requestId);
      const outcome = failureOutcome(failure);
      try {
        store.recordProviderTerminal(created.id, { owner: `${owner}:provider`, token: claimed.claim_token! }, outcome, { error: failure });
      } catch (terminalError) {
        store.audit("router", "router.provider_terminal_record_failed", "provider_invocation", created.id, { error: String(terminalError), failure });
      }
      store.audit("router", "router.provider_failure", "provider_invocation", created.id, { capability, failure });
      throw Object.assign(new ProviderError(failure.code, failure.message, provider.descriptor, requestId, { retryable: failure.retryable, details: failure.details, cause: error }), { failure });
    } finally { clearInterval(renewTimer); }
  }

  async function processEffect(
    row: EventRow,
    session: SessionCore | null,
    incident: IncidentRow,
    operatorRequestId: string,
    operatorProviderInstance: string,
    providerEffect: NonNullable<ProviderDecision["effects"]>[number],
    index: number,
    attributedCostUsd: number,
    claim: Claim,
  ): Promise<{ execution: EffectExecutionResponse; failure?: ProviderFailureResult }> {
    const proposal = wireEffectToSafety(providerEffect, row, session, incident.id, operatorRequestId, operatorProviderInstance, config, attributedCostUsd);
    let advice: PolicyAdvice | null = null;
    if (policyEnabled) {
      let resolved: ResolvedProvider;
      try {
        resolved = resolveProvider(config, "policy", selectionContext(row, session, incident.id));
      } catch (error) {
        const requestId = `router_policy_resolution_${payloadSha256({ row: row.id, incident: incident.id, index }).slice(0, 32)}`;
        const failure = failureFromUnknown(error, unresolvedDescriptor("policy"), requestId);
        const blocked = safety.authorize(proposal, null);
        return {
          execution: {
            ok: false,
            state: blocked.effect.state,
            outcome: blocked.effect.terminal_outcome,
            effect: blocked.effect,
            verdict: blocked.verdict,
            output: failure.message,
          },
          failure: { failure, resolved: undefined },
        };
      }
      const requestId = requestIdFor("policy", row, incident.id, resolved, `${operatorRequestId}:${index}`);
      const policyPacket = wireEffectFromSafety(proposal, requestId, syntheticDescriptor(resolved, "policy"), incident.id);
      try {
        const result = await invokeDurably("policy", row, incident.id, resolved, policyPacket, PolicyAdviceSchema);
        advice = result.value;
      } catch (error) {
        const failure = failureFromUnknown(error, syntheticDescriptor(resolved, "policy"), requestId);
        const blocked = safety.authorize(proposal, null);
        return {
          execution: {
            ok: false,
            state: blocked.effect.state,
            outcome: blocked.effect.terminal_outcome,
            effect: blocked.effect,
            verdict: blocked.verdict,
            output: failure.message,
          },
          failure: { failure, resolved },
        };
      }
    }
    const safetyProposal: SafetyEffectProposal = { ...proposal, provider_policy_verdict: advice?.verdict ?? null };
    // Providers cannot choose authority. Core may attach only a matching
    // durable human grant, which the kernel revalidates and atomically consumes
    // before the effect worker claims the row.
    assertClaim(row, claim);
    const grant = safety.findMatchingGrant(safetyProposal);
    const execution = await effects.execute(safetyProposal, grant?.id ?? null);
    return { execution };
  }

  async function processEvent(row: EventRow, claim: Claim): Promise<Pick<RouterTickResult, "resolved" | "escalated" | "providerFailures" | "effects" | "blockedEffects"> & { processed: 1 }> {
    assertClaim(row, claim);
    const current = store.getEvent(row.id);
    if (["resolved", "cancelled", "expired"].includes(current?.obligation_state ?? "")) {
      complete(row, claim, "obligation_closed", row.incident_id ?? undefined);
      return { processed: 1, resolved: 1, escalated: 0, providerFailures: 0, effects: 0, blockedEffects: 0 };
    }
    if (row.type === "attention.cleared") {
      const target = exactClearanceTarget(store, row);
      // Only an authenticated originating source can retire its exact native ask.
      if (target) {
        const now = store.clock.now().toISOString();
        store.db.transaction(() => {
          // Recheck the obligation and expiry inside the same transaction as
          // the resolution.  A cancellation/expiry can win after the lookup
          // above; it must never be overwritten by a late native close.
          const changed = store.db.query(
            `UPDATE events SET obligation_state='resolved'
             WHERE id=? AND source_id=? AND requires_response=1
               AND obligation_state NOT IN ('resolved','cancelled','expired')
               AND (expires_at IS NULL OR expires_at > ?)`,
          ).run(target.id, row.source_id!, now);
          if (!changed.changes) {
            store.audit("router", "obligation.clearance_ignored", "event", row.id, { reason: "target_changed_before_clearance" });
            return;
          }
          store.db.query("UPDATE human_replies SET revision=revision+1, state='resolved', resolved_at=?, updated_at=? WHERE event_id=?")
            .run(now, now, target.id);
          store.db.query("UPDATE escalations SET state='superseded' WHERE origin_event_id=? AND state IN ('pending','snoozed')").run(target.id);
          if (target.incident_id) {
            const pending = store.db.query("SELECT 1 FROM events WHERE incident_id=? AND requires_response=1 AND obligation_state NOT IN ('resolved','cancelled','expired') LIMIT 1")
              .get(target.incident_id);
            if (!pending) repo.setIncidentState(target.incident_id, "resolved");
          }
          store.audit("router", "obligation.source_cleared", "event", target.id, { clearance_event: row.id });
        })();
      } else store.audit("router", "obligation.clearance_ignored", "event", row.id, { reason: "missing_or_mismatched_exact_source_lineage" });
    }
    const session = sessionFor(store, row);
    const rule = classifyEvent(row, { now: store.clock.now(), grantedRules: [] });
    if (rule.kind === "expired") {
      store.db.query("UPDATE events SET obligation_state='expired' WHERE id=?").run(row.id);
      store.audit("router", "obligation.expired", "event", row.id, {});
      complete(row, claim, "expired");
      return { processed: 1, resolved: 1, escalated: 0, providerFailures: 0, effects: 0, blockedEffects: 0 };
    }
    if (rule.kind === "resolved" || rule.kind === "self_event") {
      complete(row, claim, rule.kind === "self_event" ? "skipped" : "router_resolved", row.incident_id ?? undefined);
      return { processed: 1, resolved: 1, escalated: 0, providerFailures: 0, effects: 0, blockedEffects: 0 };
    }
    const incident = incidentFor(row);
    if (rule.kind === "escalate") {
      escalation(row, incident, rule.reason, undefined, row.severity);
      complete(row, claim, "escalated", incident.id);
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 0, effects: 0, blockedEffects: 0 };
    }

    const context = selectionContext(row, session, incident.id);
    let memory: InvocationResult<ContextBundle>;
    let operator: InvocationResult<ProviderDecision>;
    let memoryResolved: ResolvedProvider;
    try {
      memoryResolved = resolveProvider(config, "memory", context);
    } catch (error) {
      const requestId = `router_memory_resolution_${payloadSha256({ row: row.id, incident: incident.id }).slice(0, 32)}`;
      const failure = failureFromUnknown(error, unresolvedDescriptor("memory"), requestId);
      store.audit("router", "router.provider_resolution_failure", "event", row.id, { capability: "memory", failure });
      escalation(row, incident, `memory provider could not be resolved (${failure.code}); human review required`, undefined, row.severity, { provider_failure: failure, capability: "memory" });
      complete(row, claim, "escalated", incident.id);
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 1, effects: 0, blockedEffects: 0 };
    }
    const memoryRequestId = requestIdFor("memory", row, incident.id, memoryResolved);
    const memoryInput: ContextQuery = {
      contract: "car.memory.v1",
      request_id: memoryRequestId,
      deadline_at: deadlineFor(row, store.clock.now().getTime()),
      provider: syntheticDescriptor(memoryResolved, "memory"),
      incident_id: incident.id,
      vendor: row.source_vendor,
      host: session?.host ?? row.source_host,
      ...(session?.repo_verified === 1 && session.repo ? { repo: session.repo } : {}),
      event_type: row.type,
      dedupe_class: dedupeClassFor(row),
      free_text: `${row.title}\n${row.body}`.trim().slice(0, 8_000),
      token_budget: memoryTokenBudget,
    };
    try {
      memory = await invokeDurably("memory", row, incident.id, memoryResolved, memoryInput, ContextBundleSchema);
    } catch (error) {
      const failure = failureFromUnknown(error, syntheticDescriptor(memoryResolved, "memory"), memoryRequestId);
      store.audit("router", "router.provider_failure", "provider_invocation", memoryRequestId, {
        capability: "memory",
        request_id: memoryRequestId,
        failure,
      });
      escalation(row, incident, `memory provider failed (${failure.code}); human review required`, undefined, row.severity, { provider_failure: failure, capability: "memory" });
      complete(row, claim, "escalated", incident.id);
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 1, effects: 0, blockedEffects: 0 };
    }

    let operatorResolved: ResolvedProvider;
    try {
      operatorResolved = resolveProvider(config, "operator", context);
    } catch (error) {
      const requestId = `router_operator_resolution_${payloadSha256({ row: row.id, incident: incident.id }).slice(0, 32)}`;
      const failure = failureFromUnknown(error, unresolvedDescriptor("operator"), requestId);
      store.audit("router", "router.provider_resolution_failure", "event", row.id, { capability: "operator", failure });
      escalation(row, incident, `operator provider could not be resolved (${failure.code}); human review required`, undefined, row.severity, { provider_failure: failure, capability: "operator" });
      complete(row, claim, "escalated", incident.id);
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 1, effects: 0, blockedEffects: 0 };
    }
    const operatorRequestId = requestIdFor("operator", row, incident.id, operatorResolved);
    const operatorInput: IncidentPacket = {
      contract: "car.operator.v1",
      request_id: operatorRequestId,
      deadline_at: deadlineFor(row, store.clock.now().getTime()),
      provider: syntheticDescriptor(operatorResolved, "operator"),
      incident_id: incident.id,
      car_session_id: row.car_session_id,
      dedupe_class: dedupeClassFor(row),
      events: [
        ...(store.db.query("SELECT * FROM events WHERE incident_id=? AND id != ? AND received_at <= ? ORDER BY received_at DESC, id DESC LIMIT 7")
          .all(incident.id, row.id, row.received_at) as EventRow[]).reverse(), row,
      ].map((event) => ({ id: event.id, type: event.type, severity: event.severity,
        title: event.title, body: event.body.slice(0, 4_000), requires_response: event.requires_response === 1,
        expires_at: event.expires_at, payload: event.id === row.id ? parsePayload(event) : {} })),
      context: memory.value,
    };
    try {
      operator = await invokeDurably("operator", row, incident.id, operatorResolved, operatorInput, OperatorDecision);
    } catch (error) {
      const failure = failureFromUnknown(error, syntheticDescriptor(operatorResolved, "operator"), operatorRequestId);
      store.audit("router", "router.provider_failure", "provider_invocation", operatorRequestId, {
        capability: "operator",
        request_id: operatorRequestId,
        failure,
      });
      escalation(row, incident, `operator provider failed (${failure.code}); human review required`, undefined, row.severity, { provider_failure: failure, capability: "operator" });
      complete(row, claim, "escalated", incident.id);
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 1, effects: 0, blockedEffects: 0 };
    }

    assertClaim(row, claim);
    const decisionId = repo.recordDecision({
      id: `dec_${payloadSha256({ request: operatorRequestId }).slice(0, 36)}`,
      incidentId: incident.id,
      decidedBy: "llm",
      disposition: operator.value.disposition === "resolve" ? "auto_resolve" : operator.value.disposition,
      rationale: operator.value.rationale,
      model: operator.value.model ?? null,
      tokensIn: operator.value.usage?.tokens_in,
      tokensOut: operator.value.usage?.tokens_out,
      costUsd: operator.value.usage?.cost_usd,
    });
    store.audit("router", "router.provider_decision", "decision", decisionId, {
      provider_id: operator.descriptor.provider_id,
      provider_instance: operator.descriptor.provider_instance,
      provider_request_id: operatorRequestId,
      provider_version: operator.descriptor.provider_version,
    });

    let blockedEffects = 0;
    let effectFailures = 0;
    let firstBlockedEffect: EffectExecutionResponse["effect"] | null = null;
    for (const [index, providerEffect] of operator.value.effects.entries()) {
      const effectCost = attributedEffectCost(operator.value.usage?.cost_usd, index, operator.value.effects.length);
      const result = await processEffect(row, session, incident, operatorRequestId, operator.descriptor.provider_instance, providerEffect, index, effectCost, claim);
      if (result.failure) {
        const failure = result.failure.failure;
        const failedBlockedEffects = blockedEffects + (result.execution.state === "blocked" ? 1 : 0);
        escalation(row, incident, `policy provider failed (${failure.code}); human review required`, undefined, row.severity, { provider_failure: failure, capability: "policy", decision_id: decisionId });
        complete(row, claim, "escalated", incident.id);
        recordDecisionOutcome(decisionId, row, incident.id, "escalated", { provider_failure: failure, blocked_effects: failedBlockedEffects });
        return { processed: 1, resolved: 0, escalated: 1, providerFailures: 1, effects: index + 1, blockedEffects: failedBlockedEffects };
      }
      if (!result.execution.ok) {
        blockedEffects += result.execution.state === "blocked" ? 1 : 0;
        if (result.execution.state === "blocked" && !firstBlockedEffect) firstBlockedEffect = result.execution.effect;
        effectFailures += 1;
        store.audit("router", "router.effect_blocked", "effect", result.execution.effect.intent_id, {
          decision_id: decisionId,
          verdict: result.execution.verdict,
        });
      }
    }
    const responseState = store.getEvent(row.id)?.obligation_state ?? "open";
    const responseOutstanding = row.requires_response === 1 && !["delivered", "acknowledged", "resolved", "cancelled", "expired"].includes(responseState);
    const needsEscalation = operator.value.disposition === "escalate" || blockedEffects > 0 || effectFailures > 0 ||
      (responseOutstanding && operator.value.disposition !== "defer");
    if (needsEscalation) {
      escalation(
        row,
        incident,
        blockedEffects > 0 ? "provider proposal was blocked by the core safety kernel" : responseOutstanding
          ? `A native answer is still required. Provider assessment: ${operator.value.rationale}` : operator.value.rationale,
        undefined,
        row.severity,
        { decision_id: decisionId, blocked_effects: blockedEffects },
        firstBlockedEffect ? {
          effect_intent_id: firstBlockedEffect.intent_id,
          effect_type: firstBlockedEffect.type,
          action_class: firstBlockedEffect.action_class ?? null,
        } : null,
      );
      complete(row, claim, "escalated", incident.id);
      recordDecisionOutcome(decisionId, row, incident.id, "escalated", { blocked_effects: blockedEffects, disposition: operator.value.disposition });
      return { processed: 1, resolved: 0, escalated: 1, providerFailures: 0, effects: operator.value.effects.length, blockedEffects };
    }
    if (operator.value.disposition === "defer") {
      const wakeAt = new Date(Math.min(store.clock.now().getTime() + 60_000,
        row.expires_at ? Date.parse(row.expires_at) : Infinity)).toISOString();
      // Defer means bounded human attention suppression, not request resolution.
      // Create the card now so the ordinary snooze worker can resurface it.
      const esc = escalation(row, incident, operator.value.rationale, undefined, row.severity, {}, null, false);
      store.db.query("UPDATE escalations SET state='snoozed' WHERE id=? AND state='pending'").run(esc);
      repo.setIncidentState(incident.id, "snoozed", { summary: operator.value.rationale, snoozeUntil: wakeAt });
      complete(row, claim, "deferred", incident.id);
      recordDecisionOutcome(decisionId, row, incident.id, "deferred", { disposition: operator.value.disposition });
    } else if (row.requires_response === 1 && !["resolved", "cancelled", "expired"].includes(responseState)) {
      repo.setIncidentState(incident.id, "open", { summary: "Answer delivered; awaiting source-confirmed clearance" });
      complete(row, claim, "awaiting_clearance", incident.id);
      recordDecisionOutcome(decisionId, row, incident.id, "awaiting_clearance", { disposition: operator.value.disposition });
    } else {
      repo.setIncidentState(incident.id, "resolved", { summary: operator.value.rationale });
      complete(row, claim, "provider_resolved", incident.id);
      recordDecisionOutcome(decisionId, row, incident.id, "resolved", { disposition: operator.value.disposition });
    }
    return { processed: 1, resolved: operator.value.disposition === "defer" || row.requires_response === 1 ? 0 : 1,
      escalated: 0, providerFailures: 0, effects: operator.value.effects.length, blockedEffects };
  }

  const emptyResult = (): RouterTickResult => ({ claimed: 0, processed: 0, resolved: 0, escalated: 0,
    providerFailures: 0, effects: 0, blockedEffects: 0, failed: 0 });
  async function drain(lane: "urgent" | "normal"): Promise<RouterTickResult> {
    const result = emptyResult();
    for (let n = 0; n < limit && !stopped; n++) {
      // Claim just in time, rather than letting nineteen claimed events expire
      // behind one slow model. Urgent work has a separate deterministic lane.
      const row = store.claimEvents(1, leaseSeconds, `${owner}:${lane}`, lane)[0];
      if (!row) break;
      result.claimed++;
      const claim = { owner: `${owner}:${lane}`, token: row.route_claim_token ?? row.triage_claim_token ?? "" };
      if (!claim.token) { result.failed++; continue; }
      const renewal = setInterval(() => {
        try { store.renewEventLease(row.id, claim, leaseSeconds); }
        catch (error) { store.audit("router", "router.lease_renewal_failed", "event", row.id, { error: String(error) }); }
      }, Math.max(50, leaseSeconds * 1_000 / 3));
      try {
        const processed = await processEvent(row, claim);
        for (const key of ["processed", "resolved", "escalated", "providerFailures", "effects", "blockedEffects"] as const) result[key] += processed[key];
      } catch (error) {
        result.failed++;
        store.audit("router", "router.tick_failed", "event", row.id, { error: String(error) });
      } finally { clearInterval(renewal); }
    }
    return result;
  }
  async function urgentTick(): Promise<RouterTickResult> {
    if (urgentTicking) return urgentTicking;
    urgentTicking = drain("urgent");
    try { return await urgentTicking; } finally { urgentTicking = null; }
  }
  async function tick(): Promise<RouterTickResult> {
    if (ticking) return ticking;
    ticking = (async () => {
      store.recoverExpiredClaims();
      const urgent = await urgentTick();
      const normal = await drain("normal");
      for (const key of Object.keys(urgent) as (keyof RouterTickResult)[]) normal[key] += urgent[key];
      return normal;
    })();
    try { return await ticking; } finally { ticking = null; }
  }
  const reportLoopError = (error: unknown) => store.audit("router", "router.loop_failed", "worker", owner, { error: String(error) });
  return {
    name: "attention-router", tick,
    async start(): Promise<void> {
      if (timer) return;
      stopped = false;
      urgentTimer = setInterval(() => { void urgentTick().catch(reportLoopError); }, 200);
      timer = setInterval(() => { void tick().catch(reportLoopError); }, Math.max(100, options.intervalMs ?? 1_000));
      // Startup must not await a provider while the remaining daemon workers
      // (deadlines, notifications and human delivery) have not started yet.
      void tick().catch(reportLoopError);
    },
    async stop(): Promise<void> {
      stopped = true;
      if (timer) clearInterval(timer);
      if (urgentTimer) clearInterval(urgentTimer);
      timer = undefined; urgentTimer = undefined;
      await Promise.all([ticking, urgentTicking]);
    },
  };
}

export const createAttentionRouter = createRouter;
