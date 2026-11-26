/**
 * Core-owned safety kernel for CAR v3.
 *
 * Providers can suggest a decision, but this module is the authority that
 * decides whether a typed effect may be put on the execution queue.  The
 * default ledger is intentionally small and injectable: production wires a
 * SQLite-backed implementation, while unit tests can use the in-memory
 * ledger.  No provider object, callback, or executor is accepted here.
 */
import type { Clock, EffectClaimBlockCode, EffectClaimResult, EffectClaimSafetyLimits, Store } from "../store/db.ts";

/* ------------------------------------------------------------------ identity */

export const EFFECT_TYPES = [
  "escalate",
  "notify",
  "defer",
  "reply",
  "approve",
  "deny",
  "probe",
  "run_template",
] as const;
export type EffectType = (typeof EFFECT_TYPES)[number];

export const EFFECT_STATES = ["proposed", "blocked", "pending", "running", "terminal_recorded"] as const;
export type EffectState = (typeof EFFECT_STATES)[number];
export const TERMINAL_OUTCOMES = ["ok", "failed", "cancelled", "expired", "uncertain"] as const;
export type TerminalOutcome = (typeof TERMINAL_OUTCOMES)[number];

export type GrantStatus = "active" | "consumed" | "rejected" | "revoked" | "expired";

/** A scope is valid only when its repo identity came from a verified source. */
export interface VerifiedScope {
  provider_instance?: string;
  vendor?: string;
  host?: string;
  /** Canonical VCS remote/root identity, never a cwd basename. */
  repo?: string;
  repo_verified?: boolean;
  car_session_id?: string;
  event_type?: string;
  source_id?: string;
}

/** Immutable permission lineage for one native request. */
export interface ImmutableLineage {
  source_id: string;
  request_id: string;
  /** Native tool-use/permission id, where one exists. */
  native_request_id?: string;
  event_id?: string;
  payload_sha256?: string;
  /** This is copied, never inferred from a mutable session snapshot. */
  verified_repo?: string;
}

export interface EffectProposal {
  intent_id: string;
  type: EffectType;
  args: Record<string, unknown>;
  scope: VerifiedScope;
  lineage: ImmutableLineage;
  /** Provider advice is attributable but has no authority. */
  provider_policy_verdict?: string | null;
  deadline_at?: string | null;
  /** Optional request class for rate/budget accounting. */
  action_class?: string;
  /** Optional expected cost for budget rails. */
  cost_usd?: number;
}

export interface NormalizedEffect extends EffectProposal {
  args: Record<string, unknown>;
  args_sha256: string;
  scope_key: string;
  lineage_key: string;
  dedupe_hash: string;
}

export interface GrantConstraints {
  /** Exact canonical argument object. Omit only when the grant intentionally allows any args. */
  args?: Record<string, unknown>;
  /** Optional exact action/template class. */
  action_class?: string;
  /** Optional exact deadline ceiling in milliseconds from authorization. */
  max_deadline_ms?: number;
}

export interface Grant {
  id: string;
  intent_id: string;
  lineage: ImmutableLineage | null;
  scope: VerifiedScope;
  effect_type: EffectType;
  constraints: GrantConstraints;
  status: GrantStatus;
  uses_remaining: number | null;
  created_by: "human";
  created_at: string;
  expires_at: string | null;
  consumed_at: string | null;
  revoked_at: string | null;
  provenance?: Record<string, unknown>;
}

export type GrantInput = {
  intent_id: string;
  lineage: ImmutableLineage | null;
  scope: VerifiedScope;
  effect_type: EffectType;
  constraints?: GrantConstraints;
  uses_remaining?: number | null;
  expires_at?: string | null;
  provenance?: Record<string, unknown>;
  created_by?: string;
};

export interface EffectRecord extends NormalizedEffect {
  state: EffectState;
  safety_verdict: SafetyVerdict;
  grant_id: string | null;
  proposed_at: string;
  started_at: string | null;
  finished_at: string | null;
  claim_owner: string | null;
  claim_token: string | null;
  lease_until: string | null;
  terminal_outcome: TerminalOutcome | null;
  result: unknown;
}

export type SafetyReasonCode =
  | "allowed"
  | "grant_required"
  | "grant_mismatch"
  | "grant_expired"
  | "grant_revoked"
  | "grant_consumed"
  | "panic"
  | "dangerous_content"
  | "deadline_expired"
  | "dedupe"
  | "rate_limit"
  | "budget"
  | "circuit_breaker"
  | "invalid_scope"
  | "invalid_effect"
  | "conflict"
  | "lease_lost";

export interface SafetyVerdict {
  allowed: boolean;
  code: SafetyReasonCode;
  reason: string;
  grant_id: string | null;
}

export interface SafetySnapshot {
  panic: boolean;
  panic_reason: string | null;
  breaker_open: boolean;
  attempts: number;
  failures: number;
  spent_usd: number;
}

export interface SafetyLimits {
  /** Zero means unlimited. */
  max_attempts_per_window?: number;
  attempt_window_ms?: number;
  max_failures_per_window?: number;
  failure_window_ms?: number;
  /** Rolling window for the effect cost budget; defaults to 24 hours. */
  spend_window_ms?: number;
  max_spend_usd?: number;
  dedupe_window_ms?: number;
  default_lease_ms?: number;
}

export interface SafetyLedger {
  getGrant(id: string): Grant | null;
  listGrants(): Grant[];
  putGrant(grant: Grant): void;
  getEffect(id: string): EffectRecord | null;
  putEffect(effect: EffectRecord): void;
  listEffects(): EffectRecord[];
  /** Return true when this idempotency key was newly reserved. */
  reserveIdempotency(scope: string, key: string, fingerprint: string): "inserted" | "duplicate" | "conflict";
  /** Optional standalone administrative consumption; execution uses claimEffect. */
  consumeGrant?(id: string, consumedAt: string): boolean;
  /** Atomically validate all rails, consume the grant, and claim its authorized effect. */
  claimEffect(
    id: string,
    grantId: string,
    owner: string,
    token: string,
    leaseUntil: string,
    startedAt: string,
    now: string,
    limits: EffectClaimSafetyLimits,
  ): EffectClaimResult;
  renewEffect?(id: string, owner: string, token: string, leaseUntil: string, now: string): boolean;
  recordTerminalEffect?(id: string, owner: string, token: string, outcome: TerminalOutcome, finishedAt: string, result: unknown, now: string): boolean;
  /** Optional durable panic state. Production ledgers must implement it. */
  getPanicState?(): { active: boolean; reason: string | null };
  setPanic?(reason: string): void;
  clearPanic?(): void;
}

/** Deterministic test ledger; replace with the SQLite ledger at composition time. */
export class MemorySafetyLedger implements SafetyLedger {
  private readonly grants = new Map<string, Grant>();
  private readonly grantsByIntent = new Map<string, string>();
  private readonly effects = new Map<string, EffectRecord>();
  private readonly keys = new Map<string, string>();
  private panicState: { active: boolean; reason: string | null } = { active: false, reason: null };

  getGrant(id: string): Grant | null {
    return this.grants.get(id) ?? null;
  }
  listGrants(): Grant[] {
    return [...this.grants.values()].map((g) => ({ ...g, scope: { ...g.scope }, lineage: g.lineage && { ...g.lineage } }));
  }
  putGrant(grant: Grant): void {
    const prior = this.grantsByIntent.get(grant.intent_id);
    if (prior && prior !== grant.id) {
      const existing = this.grants.get(prior);
      if (existing && grantFingerprint(existing) !== grantFingerprint(grant)) throw new SafetyError("conflict", "grant intent_id was reused for different authority");
    }
    this.grantsByIntent.set(grant.intent_id, grant.id);
    this.grants.set(grant.id, grant);
  }
  getEffect(id: string): EffectRecord | null {
    return this.effects.get(id) ?? null;
  }
  putEffect(effect: EffectRecord): void {
    this.effects.set(effect.intent_id, effect);
  }
  listEffects(): EffectRecord[] {
    return [...this.effects.values()];
  }
  reserveIdempotency(scope: string, key: string, fingerprint: string): "inserted" | "duplicate" | "conflict" {
    const k = `${scope}\u0000${key}`;
    const prior = this.keys.get(k);
    if (!prior) {
      this.keys.set(k, fingerprint);
      return "inserted";
    }
    return prior === fingerprint ? "duplicate" : "conflict";
  }
  consumeGrant(id: string, consumedAt: string): boolean {
    const grant = this.grants.get(id);
    if (!grant || grant.status !== "active" || grant.uses_remaining === null || grant.uses_remaining <= 0) return false;
    const remaining = grant.uses_remaining - 1;
    this.grants.set(id, { ...grant, uses_remaining: remaining, status: remaining === 0 ? "consumed" : "active", consumed_at: consumedAt });
    return true;
  }

  claimEffect(
    id: string,
    grantId: string,
    owner: string,
    token: string,
    leaseUntil: string,
    startedAt: string,
    now: string,
    limits: EffectClaimSafetyLimits,
  ): EffectClaimResult {
    const current = this.effects.get(id);
    const grant = this.grants.get(grantId);
    if (!current || current.state !== "pending" || current.grant_id !== grantId) {
      return { claimed: false, code: "conflict", blocked: false };
    }
    const block = (code: EffectClaimBlockCode): EffectClaimResult => {
      this.effects.set(id, {
        ...current,
        state: "blocked",
        safety_verdict: { allowed: false, code, reason: claimBlockReason(code), grant_id: current.grant_id },
      });
      return { claimed: false, code, blocked: true };
    };
    if (this.panicState.active) return block("panic");
    if (current.deadline_at != null && Date.parse(current.deadline_at) <= Date.parse(now)) return block("deadline_expired");
    if (dangerousContent(current.args)) return block("dangerous_content");
    if (!grant) return block("grant_mismatch");
    if (grant.status !== "active") {
      return block(grant.status === "revoked" ? "grant_revoked" : grant.status === "expired" ? "grant_expired" : "grant_consumed");
    }
    if (grant.expires_at !== null && Date.parse(grant.expires_at) <= Date.parse(now)) return block("grant_expired");
    if (grant.uses_remaining !== null && grant.uses_remaining <= 0) return block("grant_consumed");
    if (
      grant.effect_type !== current.type ||
      !scopeCovers(grant.scope, current.scope) ||
      (grant.lineage !== null && canonicalize(grant.lineage) !== current.lineage_key) ||
      !grant.constraints.args ||
      normalizeArgs(grant.constraints.args).sha256 !== current.args_sha256 ||
      (grant.constraints.action_class !== undefined && grant.constraints.action_class !== current.action_class)
    ) return block("grant_mismatch");
    if (grant.constraints.max_deadline_ms !== undefined) {
      const deadline = current.deadline_at == null ? Number.NaN : Date.parse(current.deadline_at);
      if (!Number.isFinite(deadline) || deadline - Date.parse(now) > grant.constraints.max_deadline_ms) return block("grant_mismatch");
    }
    const claimed = [...this.effects.values()].filter((effect) => effect.state === "running" || effect.state === "terminal_recorded");
    const at = Date.parse(now);
    if (claimed.some((effect) => effect.dedupe_hash === current.dedupe_hash && Date.parse(effect.started_at ?? effect.proposed_at) >= at - limits.dedupeWindowMs)) return block("dedupe");
    const failures = claimed.filter((effect) =>
      (effect.terminal_outcome === "failed" || effect.terminal_outcome === "uncertain") &&
      Date.parse(effect.finished_at ?? effect.started_at ?? effect.proposed_at) >= at - limits.failureWindowMs
    ).length;
    if (limits.maxFailuresPerWindow > 0 && failures >= limits.maxFailuresPerWindow) return block("circuit_breaker");
    const recentAttempts = claimed.filter((effect) => Date.parse(effect.started_at ?? effect.proposed_at) >= at - limits.attemptWindowMs).length;
    if (limits.maxAttemptsPerWindow > 0 && recentAttempts >= limits.maxAttemptsPerWindow) return block("rate_limit");
    const spent = claimed
      .filter((effect) => Date.parse(effect.started_at ?? effect.proposed_at) >= at - limits.spendWindowMs)
      .reduce((sum, effect) => sum + (effect.cost_usd ?? 0), 0);
    if (limits.maxSpendUsd > 0 && spent + (current.cost_usd ?? 0) > limits.maxSpendUsd) return block("budget");
    this.effects.set(id, { ...current, state: "running", claim_owner: owner, claim_token: token, lease_until: leaseUntil, started_at: current.started_at ?? startedAt });
    if (grant.uses_remaining !== null) {
      const remaining = grant.uses_remaining - 1;
      this.grants.set(grantId, {
        ...grant,
        uses_remaining: remaining,
        status: remaining === 0 ? "consumed" : "active",
        consumed_at: remaining === 0 ? now : grant.consumed_at,
      });
    }
    return { claimed: true };
  }

  renewEffect(id: string, owner: string, token: string, leaseUntil: string, now: string): boolean {
    const current = this.effects.get(id);
    if (!current || current.state !== "running" || current.claim_owner !== owner || current.claim_token !== token || (current.lease_until !== null && Date.parse(current.lease_until) <= Date.parse(now))) return false;
    this.effects.set(id, { ...current, lease_until: leaseUntil });
    return true;
  }

  recordTerminalEffect(id: string, owner: string, token: string, outcome: TerminalOutcome, finishedAt: string, result: unknown, now: string): boolean {
    const current = this.effects.get(id);
    if (!current || current.state !== "running" || current.claim_owner !== owner || current.claim_token !== token || (current.lease_until !== null && Date.parse(current.lease_until) <= Date.parse(now))) return false;
    this.effects.set(id, { ...current, state: "terminal_recorded", terminal_outcome: outcome, result, finished_at: finishedAt, lease_until: null });
    return true;
  }
  getPanicState(): { active: boolean; reason: string | null } {
    return { ...this.panicState };
  }
  setPanic(reason: string): void {
    this.panicState = { active: true, reason };
  }
  clearPanic(): void {
    this.panicState = { active: false, reason: null };
  }
}

function grantFingerprint(grant: Grant): string {
  return canonicalize({
    intent_id: grant.intent_id,
    lineage: grant.lineage,
    scope: grant.scope,
    effect_type: grant.effect_type,
    constraints: grant.constraints,
    uses_remaining: grant.uses_remaining,
    expires_at: grant.expires_at,
  });
}

/**
 * SQLite ledger adapter used by the composition root once the v3 lifecycle
 * migration is active. Keeping this adapter behind SafetyLedger lets tests use
 * the deterministic memory ledger without weakening the production contract.
 */
export class SqlSafetyLedger implements SafetyLedger {
  constructor(private readonly store: Store) {}

  getGrant(id: string): Grant | null {
    const row = this.store.getGrant(id);
    return row ? grantFromRow(row as unknown as Record<string, unknown>) : null;
  }

  listGrants(): Grant[] {
    return this.store.listGrants().map((row) => grantFromRow(row as unknown as Record<string, unknown>));
  }

  putGrant(grant: Grant): void {
    this.store.upsertGrant({
      id: grant.id,
      intentId: grant.intent_id,
      lineageId: grant.lineage ? canonicalize(grant.lineage) : null,
      scope: { ...grant.scope },
      effectType: grant.effect_type,
      constraints: { ...grant.constraints },
      status: grant.status,
      usesRemaining: grant.uses_remaining,
      createdBy: grant.created_by,
      expiresAt: grant.expires_at,
      consumedAt: grant.consumed_at,
      revokedAt: grant.revoked_at,
      provenance: grant.provenance,
    });
  }

  getEffect(id: string): EffectRecord | null {
    const row = this.store.getEffectByIntent(id);
    return row ? effectFromRow(row as unknown as Record<string, unknown>) : null;
  }

  putEffect(effect: EffectRecord): void {
    const existing = this.store.getEffectByIntent(effect.intent_id);
    if (existing && effect.state === "pending") {
      this.store.authorizeEffect(
        effect.intent_id,
        effect.grant_id,
        effect.safety_verdict.code,
        effect.provider_policy_verdict,
      );
      return;
    }
    if (existing && effect.state === "blocked") {
      this.store.blockEffectByIntent(effect.intent_id, effect.safety_verdict.code, effect.safety_verdict);
      return;
    }
    this.store.upsertEffect({
      id: effectIdFor(effect),
      intentId: effect.intent_id,
      type: effect.type,
      args: effect.args,
      lineageId: effect.lineage_key,
      lineage: { ...effect.lineage },
      scope: { ...effect.scope },
      deadlineAt: effect.deadline_at,
      actionClass: effect.action_class ?? null,
      costUsd: effect.cost_usd ?? 0,
      providerPolicyVerdict: effect.provider_policy_verdict ?? null,
      safetyVerdict: effect.safety_verdict.code,
      grantId: effect.grant_id,
      dedupeHash: effect.dedupe_hash,
      state: effect.state,
      terminalOutcome: effect.terminal_outcome,
      claimOwner: effect.claim_owner,
      claimToken: effect.claim_token,
      leaseUntil: effect.lease_until,
      startedAt: effect.started_at,
      finishedAt: effect.finished_at,
      result: effect.result,
    });
  }

  listEffects(): EffectRecord[] {
    return this.store.listEffects().map((row) => effectFromRow(row as unknown as Record<string, unknown>));
  }

  reserveIdempotency(scope: string, key: string, fingerprint: string): "inserted" | "duplicate" | "conflict" {
    return this.store.reserveIdempotency(scope, key, fingerprint, "safety", key);
  }

  consumeGrant(id: string, _consumedAt: string): boolean {
    return this.store.consumeGrant(id);
  }

  claimEffect(
    id: string,
    grantId: string,
    owner: string,
    token: string,
    leaseUntil: string,
    startedAt: string,
    now: string,
    limits: EffectClaimSafetyLimits,
  ): EffectClaimResult {
    return this.store.claimEffect(id, grantId, owner, token, leaseUntil, startedAt, now, limits);
  }

  renewEffect(id: string, owner: string, token: string, leaseUntil: string, now: string): boolean {
    return this.store.renewEffect(id, owner, token, leaseUntil, now);
  }

  recordTerminalEffect(id: string, owner: string, token: string, outcome: TerminalOutcome, finishedAt: string, result: unknown, now: string): boolean {
    return this.store.recordTerminalEffect(id, owner, token, outcome, finishedAt, result, now);
  }

  getPanicState(): { active: boolean; reason: string | null } {
    return this.store.getPanicState();
  }

  setPanic(reason: string): void {
    this.store.setPanic(reason);
  }

  clearPanic(): void {
    this.store.clearPanic();
  }
}

function jsonObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    return isPlainRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function grantFromRow(row: Record<string, unknown>): Grant {
  const status = String(row.status ?? "active") as GrantStatus;
  return {
    id: String(row.id),
    intent_id: String(row.intent_id),
    lineage: parseLineage(row.lineage_id),
    scope: jsonObject(row.scope_json),
    effect_type: String(row.effect_type) as EffectType,
    constraints: jsonObject(row.constraint_json),
    status,
    uses_remaining: row.uses_remaining === null || row.uses_remaining === undefined ? null : Number(row.uses_remaining),
    created_by: "human",
    created_at: String(row.created_at),
    expires_at: row.expires_at == null ? null : String(row.expires_at),
    consumed_at: row.consumed_at == null ? null : String(row.consumed_at),
    revoked_at: row.revoked_at == null ? null : String(row.revoked_at),
    provenance: jsonObject(row.provenance_json),
  };
}

function effectIdFor(effect: EffectRecord): string {
  // The intent is the stable idempotency identity. The SQL row id is only a
  // storage key and is deterministic across an upsert/replay in this adapter.
  return `eff_${sha256(effect.intent_id).slice(0, 26)}`;
}

function effectFromRow(row: Record<string, unknown>): EffectRecord {
  const args = jsonObject(row.args_json);
  const scope = jsonObject(row.scope_json);
  const lineage = parseLineage(row.lineage_json) ?? parseLineage(row.lineage_id) ?? {
    source_id: "unknown",
    request_id: String(row.intent_id),
  };
  const state = String(row.state) as EffectState;
  const verdictCode = String(row.safety_verdict) as SafetyReasonCode;
  return {
    intent_id: String(row.intent_id),
    type: String(row.type) as EffectType,
    args,
    scope,
    lineage,
    provider_policy_verdict: row.provider_policy_verdict == null ? null : String(row.provider_policy_verdict),
    deadline_at: row.deadline_at == null ? null : String(row.deadline_at),
    action_class: row.action_class == null ? undefined : String(row.action_class),
    cost_usd: row.cost_usd == null ? 0 : Number(row.cost_usd),
    args_sha256: String(row.args_sha256),
    scope_key: canonicalize(scope),
    lineage_key: canonicalize(lineage),
    dedupe_hash: String(row.dedupe_hash ?? ""),
    state,
    safety_verdict: {
      allowed: state === "pending" || state === "running" || state === "terminal_recorded",
      code: verdictCode,
      reason: verdictCode,
      grant_id: row.grant_id == null ? null : String(row.grant_id),
    },
    grant_id: row.grant_id == null ? null : String(row.grant_id),
    proposed_at: String(row.created_at),
    started_at: row.started_at == null ? null : String(row.started_at),
    finished_at: row.finished_at == null ? null : String(row.finished_at),
    claim_owner: row.claim_owner == null ? null : String(row.claim_owner),
    claim_token: row.claim_token == null ? null : String(row.claim_token),
    lease_until: row.lease_until == null ? null : String(row.lease_until),
    terminal_outcome: row.terminal_outcome == null ? null : String(row.terminal_outcome) as TerminalOutcome,
    result: row.result_json == null ? null : JSON.parse(String(row.result_json)),
  };
}

function parseLineage(value: unknown): ImmutableLineage | null {
  if (typeof value !== "string" || !value) return null;
  try {
    const parsed = JSON.parse(value);
    return isPlainRecord(parsed) && typeof parsed.source_id === "string" && typeof parsed.request_id === "string"
      ? (parsed as unknown as ImmutableLineage)
      : { source_id: "legacy", request_id: value };
  } catch {
    return { source_id: "legacy", request_id: value };
  }
}

const DEFAULT_CLOCK: Clock = { now: () => new Date() };

export interface SafetyKernelOptions {
  ledger?: SafetyLedger;
  clock?: Clock;
  limits?: SafetyLimits;
  audit?: (verb: string, objectId: string, detail?: unknown) => void;
}

/* ---------------------------------------------------------- canonicalization */

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Stable JSON with recursively sorted object keys and no ambiguous values. */
export function canonicalize(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite numbers are not valid canonical values");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map((item) => canonicalize(item)).join(",")}]`;
  if (isPlainRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => {
        const item = value[key];
        if (item === undefined) throw new Error(`undefined value at '${key}' is not canonical`);
        return `${JSON.stringify(key)}:${canonicalize(item)}`;
      })
      .join(",")}}`;
  }
  throw new Error(`unsupported canonical value '${typeof value}'`);
}

export function sha256(value: string): string {
  return new Bun.CryptoHasher("sha256").update(value).digest("hex");
}

export function normalizeArgs(args: Record<string, unknown>): { value: Record<string, unknown>; key: string; sha256: string } {
  const key = canonicalize(args);
  return { value: JSON.parse(key) as Record<string, unknown>, key, sha256: sha256(key) };
}

/** Named aliases used by adapters and contract tests. */
export const canonicalJson = canonicalize;
export const normalizeEffectArgs = normalizeArgs;

function normalizeScope(scope: VerifiedScope): { value: VerifiedScope; key: string } {
  if (scope.repo !== undefined && !scope.repo_verified) {
    throw new SafetyError("invalid_scope", "repo scope requires a verified VCS identity");
  }
  const value = JSON.parse(canonicalize(scope)) as VerifiedScope;
  return { value, key: canonicalize(value) };
}

function normalizeLineage(lineage: ImmutableLineage): { value: ImmutableLineage; key: string } {
  if (!lineage.source_id || !lineage.request_id) {
    throw new SafetyError("invalid_effect", "immutable lineage requires source_id and request_id");
  }
  const value = JSON.parse(canonicalize(lineage)) as ImmutableLineage;
  return { value, key: canonicalize(value) };
}

export function normalizeEffect(proposal: EffectProposal): NormalizedEffect {
  if (!EFFECT_TYPES.includes(proposal.type)) throw new SafetyError("invalid_effect", `unknown effect type '${proposal.type}'`);
  if (!proposal.intent_id) throw new SafetyError("invalid_effect", "intent_id is required");
  if (proposal.cost_usd !== undefined && (!Number.isFinite(proposal.cost_usd) || proposal.cost_usd < 0)) {
    throw new SafetyError("invalid_effect", "cost_usd must be a finite nonnegative number");
  }
  const args = normalizeArgs(proposal.args);
  const scope = normalizeScope(proposal.scope);
  const lineage = normalizeLineage(proposal.lineage);
  const dedupe_hash = sha256(
    [proposal.type, args.key, scope.key, lineage.key].join("\u001f"),
  );
  return {
    ...proposal,
    args: args.value,
    args_sha256: args.sha256,
    scope: scope.value,
    scope_key: scope.key,
    lineage: lineage.value,
    lineage_key: lineage.key,
    dedupe_hash,
  };
}

function nowIso(clock: Clock): string {
  return clock.now().toISOString();
}

function uuid(prefix: string): string {
  return `${prefix}_${crypto.randomUUID()}`;
}

export class SafetyError extends Error {
  constructor(readonly code: SafetyReasonCode, message: string) {
    super(message);
    this.name = "SafetyError";
  }
}

function dangerousText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(dangerousText).join(" ");
  if (isPlainRecord(value)) return Object.values(value).map(dangerousText).join(" ");
  return "";
}

/** Non-bypassable content rail. Provider prose and grants are never inputs. */
export const NEVER_AUTO_APPROVE: { pattern: RegExp; label: string }[] = [
  { pattern: /\bgit\b[^\n]*\bpush\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b)/i, label: "force push" },
  { pattern: /\bgit\b[^\n]*\breset\b[^\n]*--hard/i, label: "git reset --hard" },
  { pattern: /\bgit\b[^\n]*\bclean\b[^\n]*-[a-z]*f/i, label: "git clean -f" },
  { pattern: /\brm\b[^\n]*-[a-z]*r[a-z]*f|\brm\b[^\n]*-[a-z]*f[a-z]*r/i, label: "recursive force delete" },
  { pattern: /\bsudo\b/i, label: "sudo" },
  { pattern: /\b(curl|wget)\b[^\n]*\|[^\n]*\b(sh|bash|zsh)\b/i, label: "pipe-to-shell" },
  { pattern: /\bchmod\b[^\n]*\b777\b/i, label: "chmod 777" },
  { pattern: /\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b/i, label: "merging a pull request" },
  { pattern: /\b(npm|bun|yarn|pnpm)\b[^\n]*\bpublish\b/i, label: "publishing a package" },
  { pattern: /\bterraform\b[^\n]*\b(apply|destroy)\b/i, label: "terraform apply/destroy" },
  { pattern: /\bkubectl\b[^\n]*\bdelete\b/i, label: "kubectl delete" },
  { pattern: /\bdrop\s+(table|database)\b/i, label: "dropping a database object" },
  { pattern: /\b(prod|production)\b[^\n]*\b(deploy|restart|delete|drop)\b/i, label: "production mutation" },
  { pattern: /(^|[\s/'"])\.env(\.[\w-]+)?([\s/'"]|$)|\bid_rsa\b|\b(credentials|secrets?)\.(json|ya?ml|toml)\b/i, label: "credentials or secrets" },
];

export function dangerousContent(value: unknown): string | null {
  const text = dangerousText(value);
  return NEVER_AUTO_APPROVE.find(({ pattern }) => pattern.test(text))?.label ?? null;
}

function claimBlockReason(code: SafetyReasonCode): string {
  switch (code) {
    case "panic": return "panic mode is active at execution claim";
    case "dangerous_content": return "non-bypassable content rail rejected execution claim";
    case "deadline_expired": return "effect deadline expired before execution";
    case "grant_required": return "an explicit active human grant is required";
    case "grant_mismatch": return "grant no longer matches the exact effect at execution claim";
    case "grant_expired": return "grant expired before execution claim";
    case "grant_revoked": return "grant was revoked before execution claim";
    case "grant_consumed": return "grant authority was already consumed";
    case "dedupe": return "an identical effect already crossed the execution boundary within the dedupe window";
    case "rate_limit": return "effect rate limit was exhausted at execution claim";
    case "budget": return "effect spend budget was exhausted at execution claim";
    case "circuit_breaker": return "failure circuit breaker opened before execution claim";
    case "conflict": return "effect claim lost a concurrent state transition";
    default: return code;
  }
}

/* -------------------------------------------------------------- kernel */

export interface SafetyKernel {
  readonly ledger: SafetyLedger;
  createGrant(input: GrantInput): Grant;
  revokeGrant(grantId: string, reason?: string): boolean;
  getGrant(id: string): Grant | null;
  listGrants(): Grant[];
  /**
   * Select the most specific active human grant matching this exact proposal.
   * Providers never name or choose grants; authorize() still revalidates and
   * atomically consumes the returned authority.
   */
  findMatchingGrant(effect: EffectProposal): Grant | null;
  propose(effect: EffectProposal): EffectRecord;
  authorize(effect: EffectProposal, grantId?: string | null): { effect: EffectRecord; verdict: SafetyVerdict };
  claim(effectId: string, owner: string, leaseMs?: number): { effect: EffectRecord; token: string } | null;
  renew(effectId: string, owner: string, token: string, leaseMs?: number): boolean;
  recordTerminal(effectId: string, owner: string, token: string, outcome: TerminalOutcome, result?: unknown): EffectRecord;
  panic(reason?: string): void;
  clearPanic(): void;
  snapshot(): SafetySnapshot;
}

export function createSafetyKernel(options: SafetyKernelOptions = {}): SafetyKernel {
  const ledger = options.ledger ?? new MemorySafetyLedger();
  const clock = options.clock ?? DEFAULT_CLOCK;
  const limits: Required<SafetyLimits> = {
    max_attempts_per_window: options.limits?.max_attempts_per_window ?? 0,
    attempt_window_ms: options.limits?.attempt_window_ms ?? 60 * 60_000,
    max_failures_per_window: options.limits?.max_failures_per_window ?? 5,
    failure_window_ms: options.limits?.failure_window_ms ?? 10 * 60_000,
    spend_window_ms: options.limits?.spend_window_ms ?? 24 * 60 * 60_000,
    max_spend_usd: options.limits?.max_spend_usd ?? 0,
    dedupe_window_ms: options.limits?.dedupe_window_ms ?? 30 * 60_000,
    default_lease_ms: options.limits?.default_lease_ms ?? 120_000,
  };
  const restoredPanic = ledger.getPanicState?.() ?? { active: false, reason: null };
  let panicMode = restoredPanic.active;
  let panicReason: string | null = restoredPanic.reason;
  const audit = (verb: string, objectId: string, detail: unknown = {}): void => options.audit?.(verb, objectId, detail);
  const now = (): Date => clock.now();
  /** Re-read canonical effect rows for every gate, including after restart. */
  const attempts = (): { at: number; hash: string; cost: number; failed: boolean }[] => ledger.listEffects()
    .filter((effect) => effect.state !== "proposed" && effect.state !== "blocked")
    .map((effect) => ({
      at: Date.parse(effect.finished_at ?? effect.started_at ?? effect.proposed_at),
      hash: effect.dedupe_hash,
      cost: effect.cost_usd ?? 0,
      failed: effect.terminal_outcome === "failed" || effect.terminal_outcome === "uncertain",
    }))
    .filter((effect) => Number.isFinite(effect.at));
  const currentFailures = (at: number): number => attempts().filter((a) => a.failed && a.at >= at - limits.failure_window_ms).length;
  const currentSpend = (at: number): number =>
    attempts()
      .filter((item) => item.at >= at - limits.spend_window_ms)
      .reduce((sum, item) => sum + item.cost, 0);
  const breakerOpen = (): boolean => limits.max_failures_per_window > 0 && currentFailures(now().getTime()) >= limits.max_failures_per_window;
  const claimLimits: EffectClaimSafetyLimits = {
    maxAttemptsPerWindow: limits.max_attempts_per_window,
    attemptWindowMs: limits.attempt_window_ms,
    maxFailuresPerWindow: limits.max_failures_per_window,
    failureWindowMs: limits.failure_window_ms,
    spendWindowMs: limits.spend_window_ms,
    maxSpendUsd: limits.max_spend_usd,
    dedupeWindowMs: limits.dedupe_window_ms,
    dangerousArgsSha256: null,
  };

  function createGrant(input: GrantInput): Grant {
    if (input.created_by !== undefined && input.created_by !== "human" && input.created_by !== "david") {
      throw new SafetyError("invalid_effect", "only an explicit human action may create a grant");
    }
    normalizeScope(input.scope);
    if (Object.keys(input.scope).length === 0) {
      throw new SafetyError("invalid_scope", "a grant must declare at least one verified scope constraint");
    }
    if (!EFFECT_TYPES.includes(input.effect_type)) throw new SafetyError("invalid_effect", "unknown grant effect type");
    const usesRemaining = input.uses_remaining ?? null;
    if (usesRemaining !== null && (!Number.isInteger(usesRemaining) || usesRemaining <= 0)) {
      throw new SafetyError("invalid_effect", "uses_remaining must be a positive integer or null");
    }
    if (usesRemaining !== null && !input.lineage) {
      throw new SafetyError("invalid_effect", "a one-shot or bounded grant must bind immutable request lineage");
    }
    if (input.lineage) normalizeLineage(input.lineage);
    const at = nowIso(clock);
    const grant: Grant = {
      intent_id: input.intent_id,
      scope: JSON.parse(canonicalize(input.scope)) as VerifiedScope,
      effect_type: input.effect_type,
      expires_at: input.expires_at ?? null,
      provenance: input.provenance,
      uses_remaining: usesRemaining,
      id: uuid("grant"),
      status: "active",
      created_by: "human",
      created_at: at,
      consumed_at: null,
      revoked_at: null,
      lineage: input.lineage ? JSON.parse(canonicalize(input.lineage)) as ImmutableLineage : null,
      constraints: input.constraints?.args ? { ...input.constraints, args: normalizeArgs(input.constraints.args).value } : { ...(input.constraints ?? {}) },
    };
    ledger.putGrant(grant);
    audit("grant.created", grant.id, { effect_type: grant.effect_type, intent_id: grant.intent_id });
    return grant;
  }

  function revokeGrant(grantId: string, reason = "revoked by human"): boolean {
    const grant = ledger.getGrant(grantId);
    if (!grant || grant.status !== "active") return false;
    const next: Grant = { ...grant, status: "revoked", revoked_at: nowIso(clock) };
    ledger.putGrant(next);
    audit("grant.revoked", grantId, { reason });
    return true;
  }

  function grantVerdict(effect: NormalizedEffect, grantId?: string | null): { verdict: SafetyVerdict; grant: Grant | null } {
    if (!grantId) return { grant: null, verdict: { allowed: false, code: "grant_required", reason: "an explicit active human grant is required", grant_id: null } };
    const grant = ledger.getGrant(grantId);
    if (!grant) return { grant: null, verdict: { allowed: false, code: "grant_mismatch", reason: "grant does not exist", grant_id: grantId } };
    if (grant.status !== "active") {
      const code: SafetyReasonCode = grant.status === "expired" ? "grant_expired" : grant.status === "revoked" ? "grant_revoked" : "grant_consumed";
      return { grant, verdict: { allowed: false, code, reason: `grant is ${grant.status}`, grant_id: grant.id } };
    }
    if (grant.expires_at && Date.parse(grant.expires_at) <= now().getTime()) {
      ledger.putGrant({ ...grant, status: "expired" });
      audit("grant.expired", grant.id);
      return { grant, verdict: { allowed: false, code: "grant_expired", reason: "grant has expired", grant_id: grant.id } };
    }
    if (grant.effect_type !== effect.type || !scopeCovers(grant.scope, effect.scope)) {
      return { grant, verdict: { allowed: false, code: "grant_mismatch", reason: "effect type or verified scope does not match grant", grant_id: grant.id } };
    }
    // Bounded/one-shot authority is always tied to the immutable request it
    // was created to answer. A reusable grant may explicitly carry no
    // grant-level lineage; that is reviewed cross-request authority inside
    // the otherwise exact verified scope, effect type, and arguments.
    if (grant.lineage && canonicalize(grant.lineage) !== effect.lineage_key) {
      return { grant, verdict: { allowed: false, code: "grant_mismatch", reason: "immutable request lineage does not match grant", grant_id: grant.id } };
    }
    if (!grant.constraints.args || normalizeArgs(grant.constraints.args).sha256 !== effect.args_sha256) {
      return { grant, verdict: { allowed: false, code: "grant_mismatch", reason: "normalized effect arguments do not match grant", grant_id: grant.id } };
    }
    if (grant.constraints.action_class && grant.constraints.action_class !== effect.action_class) {
      return { grant, verdict: { allowed: false, code: "grant_mismatch", reason: "action class does not match grant", grant_id: grant.id } };
    }
    if (grant.constraints.max_deadline_ms !== undefined) {
      const deadline = effect.deadline_at ? Date.parse(effect.deadline_at) : Number.NaN;
      if (!Number.isFinite(deadline) || deadline - now().getTime() > grant.constraints.max_deadline_ms) {
        return { grant, verdict: { allowed: false, code: "grant_mismatch", reason: "effect deadline exceeds the grant ceiling", grant_id: grant.id } };
      }
    }
    return { grant, verdict: { allowed: true, code: "allowed", reason: "matching human grant", grant_id: grant.id } };
  }

  function findMatchingGrant(effect: EffectProposal): Grant | null {
    const normalized = normalizeEffect(effect);
    const candidates = ledger.listGrants()
      .filter((grant) => grant.status === "active")
      .filter((grant) => !grant.expires_at || Date.parse(grant.expires_at) > now().getTime())
      .filter((grant) => grantVerdict(normalized, grant.id).verdict.allowed)
      .sort((left, right) => {
        // Exact-lineage authority outranks reusable authority. Within that,
        // prefer the narrowest scope and a bounded grant, then a stable id.
        const lineage = Number(Boolean(right.lineage)) - Number(Boolean(left.lineage));
        if (lineage !== 0) return lineage;
        const scope = Object.keys(right.scope).length - Object.keys(left.scope).length;
        if (scope !== 0) return scope;
        const bounded = Number(right.uses_remaining !== null) - Number(left.uses_remaining !== null);
        if (bounded !== 0) return bounded;
        return left.id.localeCompare(right.id);
      });
    return candidates[0] ?? null;
  }

  function put(effect: EffectRecord): EffectRecord {
    ledger.putEffect(effect);
    return effect;
  }

  function propose(effect: EffectProposal): EffectRecord {
    const normalized = normalizeEffect(effect);
    const prior = ledger.getEffect(normalized.intent_id);
    if (prior) {
      if (prior.dedupe_hash !== normalized.dedupe_hash) throw new SafetyError("conflict", "intent_id was reused for different effect");
      return prior;
    }
    const record: EffectRecord = {
      ...normalized,
      state: "proposed",
      safety_verdict: { allowed: false, code: "grant_required", reason: "not yet authorized", grant_id: null },
      grant_id: null,
      proposed_at: nowIso(clock),
      started_at: null,
      finished_at: null,
      claim_owner: null,
      claim_token: null,
      lease_until: null,
      terminal_outcome: null,
      result: null,
    };
    audit("effect.proposed", record.intent_id, { type: record.type, args_sha256: record.args_sha256 });
    return put(record);
  }

  function authorize(effect: EffectProposal, grantId?: string | null): { effect: EffectRecord; verdict: SafetyVerdict } {
    const record = propose(effect);
    if (record.state === "terminal_recorded" || record.state === "running" || record.state === "pending") {
      return { effect: record, verdict: record.safety_verdict };
    }
    const normalized = record;
    let verdict: SafetyVerdict;
    if (panicMode) verdict = { allowed: false, code: "panic", reason: panicReason ?? "panic mode is active", grant_id: null };
    else if (normalized.deadline_at && Date.parse(normalized.deadline_at) <= now().getTime()) {
      verdict = { allowed: false, code: "deadline_expired", reason: "effect deadline has expired", grant_id: null };
    } else {
      const danger = dangerousContent(normalized.args);
      if (danger) verdict = { allowed: false, code: "dangerous_content", reason: `non-bypassable rail: ${danger}`, grant_id: null };
      else {
        // Check authority before throughput gates so a replay of a consumed
        // one-shot grant reports the authority failure, not an incidental
        // dedupe hit. This keeps the human decision explainable and auditable.
        const authority = grantVerdict(normalized, grantId).verdict;
        if (!authority.allowed) verdict = authority;
        else if (attempts().some((a) => a.hash === normalized.dedupe_hash && a.at >= now().getTime() - limits.dedupe_window_ms)) verdict = { allowed: false, code: "dedupe", reason: "identical effect was already authorized within the dedupe window", grant_id: null };
        else if (breakerOpen()) verdict = { allowed: false, code: "circuit_breaker", reason: "failure circuit breaker is open", grant_id: null };
        else if (limits.max_attempts_per_window > 0 && attempts().filter((a) => a.at >= now().getTime() - limits.attempt_window_ms).length >= limits.max_attempts_per_window) {
          verdict = { allowed: false, code: "rate_limit", reason: "effect rate limit is exhausted", grant_id: null };
        } else if (limits.max_spend_usd > 0 && currentSpend(now().getTime()) + (normalized.cost_usd ?? 0) > limits.max_spend_usd) {
          verdict = { allowed: false, code: "budget", reason: "effect budget is exhausted", grant_id: null };
        } else verdict = authority;
      }
    }
    const next: EffectRecord = {
      ...normalized,
      state: verdict.allowed ? "pending" : "blocked",
      safety_verdict: verdict,
      grant_id: verdict.grant_id,
    };
    // Grant consumption is deliberately deferred to claim(), where durable
    // ledgers reserve authority and the effect worker in one transaction.
    // A crash after authorization therefore cannot burn a one-shot grant.
    audit(verdict.allowed ? "effect.authorized" : "effect.blocked", normalized.intent_id, verdict);
    return { effect: put(next), verdict };
  }

  function claim(effectId: string, owner: string, leaseMs = limits.default_lease_ms): { effect: EffectRecord; token: string } | null {
    const existing = ledger.getEffect(effectId);
    if (!existing) return null;
    if (existing.state !== "pending") return null;
    const at = now();
    if (!existing.grant_id) {
      const verdict: SafetyVerdict = { allowed: false, code: "grant_required", reason: claimBlockReason("grant_required"), grant_id: null };
      put({ ...existing, state: "blocked", safety_verdict: verdict });
      audit("effect.blocked_before_claim", effectId, verdict);
      return null;
    }
    const token = uuid("claim");
    const next: EffectRecord = { ...existing, state: "running", claim_owner: owner, claim_token: token, lease_until: new Date(at.getTime() + leaseMs).toISOString(), started_at: existing.started_at ?? at.toISOString() };
    const result = ledger.claimEffect(
      effectId,
      existing.grant_id,
      owner,
      token,
      next.lease_until!,
      next.started_at!,
      at.toISOString(),
      {
        ...claimLimits,
        dangerousArgsSha256: dangerousContent(existing.args) ? existing.args_sha256 : null,
      },
    );
    if (!result.claimed) {
      const refreshed = ledger.getEffect(effectId);
      if (result.blocked && refreshed?.state === "blocked") {
        const verdict: SafetyVerdict = {
          allowed: false,
          code: result.code,
          reason: claimBlockReason(result.code),
          grant_id: refreshed.grant_id,
        };
        // SQL persists the reason code atomically; the adapter's typed view
        // reconstructs the explanatory text here for callers and audit.
        audit("effect.blocked_before_claim", effectId, verdict);
      }
      return null;
    }
    audit("effect.claimed", effectId, { owner, lease_until: next.lease_until });
    return { effect: ledger.getEffect(effectId) ?? next, token };
  }

  function renew(effectId: string, owner: string, token: string, leaseMs = limits.default_lease_ms): boolean {
    const existing = ledger.getEffect(effectId);
    if (!existing || existing.state !== "running" || existing.claim_owner !== owner || existing.claim_token !== token) return false;
    if (existing.lease_until && Date.parse(existing.lease_until) <= now().getTime()) return false;
    const leaseUntil = new Date(now().getTime() + leaseMs).toISOString();
    if (ledger.renewEffect) return ledger.renewEffect(effectId, owner, token, leaseUntil, nowIso(clock));
    put({ ...existing, lease_until: leaseUntil });
    return true;
  }

  function recordTerminal(effectId: string, owner: string, token: string, outcome: TerminalOutcome, result: unknown = null): EffectRecord {
    if (!TERMINAL_OUTCOMES.includes(outcome)) throw new SafetyError("invalid_effect", `unknown terminal outcome '${outcome}'`);
    const existing = ledger.getEffect(effectId);
    if (!existing) throw new SafetyError("invalid_effect", "unknown effect");
    if (existing.state === "terminal_recorded") {
      if (existing.terminal_outcome !== outcome) throw new SafetyError("conflict", "effect already has a different terminal outcome");
      return existing;
    }
    if (existing.state !== "running" || existing.claim_owner !== owner || existing.claim_token !== token) throw new SafetyError("lease_lost", "effect claim is not owned by this worker");
    if (existing.lease_until && Date.parse(existing.lease_until) <= now().getTime()) throw new SafetyError("lease_lost", "effect claim lease has expired");
    const next: EffectRecord = { ...existing, state: "terminal_recorded", terminal_outcome: outcome, result, finished_at: nowIso(clock), lease_until: null };
    if (ledger.recordTerminalEffect && !ledger.recordTerminalEffect(effectId, owner, token, outcome, next.finished_at!, result, nowIso(clock))) throw new SafetyError("lease_lost", "effect claim was lost before terminal recording");
    const item = ledger.recordTerminalEffect ? (ledger.getEffect(effectId) ?? next) : put(next);
    audit("effect.terminal_recorded", effectId, { outcome });
    return item;
  }

  return {
    ledger,
    createGrant,
    revokeGrant,
    getGrant: (id) => ledger.getGrant(id),
    listGrants: () => ledger.listGrants(),
    findMatchingGrant,
    propose,
    authorize,
    claim,
    renew,
    recordTerminal,
    panic(reason = "manual panic") {
      panicMode = true;
      panicReason = reason;
      ledger.setPanic?.(reason);
      // Pending work has not crossed the external-world boundary yet, so it
      // can be safely cancelled. A running adapter remains fenced and must
      // record its own terminal result (possibly uncertain).
      for (const effect of ledger.listEffects()) {
        if (effect.state === "pending") {
          put({ ...effect, state: "blocked", safety_verdict: { allowed: false, code: "panic", reason, grant_id: effect.grant_id } });
          audit("effect.blocked", effect.intent_id, { code: "panic", reason });
        }
      }
      audit("safety.panic", "global", { reason });
    },
    clearPanic() {
      panicMode = false;
      panicReason = null;
      ledger.clearPanic?.();
      audit("safety.panic_cleared", "global");
    },
    snapshot(): SafetySnapshot {
      const at = now().getTime();
      return { panic: panicMode, panic_reason: panicReason, breaker_open: breakerOpen(), attempts: attempts().length, failures: currentFailures(at), spent_usd: currentSpend(at) };
    },
  };
}

/** Grant scope is an explicit constraint set; extra verified effect context is allowed. */
export function scopeCovers(grantScope: VerifiedScope, effectScope: VerifiedScope): boolean {
  return Object.entries(grantScope).every(([key, value]) => {
    const actual = effectScope[key as keyof VerifiedScope];
    return actual !== undefined && canonicalize(actual) === canonicalize(value);
  });
}
