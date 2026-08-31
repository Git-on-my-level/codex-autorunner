/**
 * Store root: opens SQLite (WAL), applies migrations, exposes typed repositories.
 *
 * Schema is FROZEN (see migrations.ts). Repository interfaces below are stable;
 * workstreams may run additional read queries against `store.db` from within
 * their own modules, but all writes MUST go through a repository or be added
 * here by the coordinator, and every state-changing write MUST audit.
 */
import { Database } from "bun:sqlite";
import { MIGRATIONS } from "./migrations.ts";
import type { CarEvent, SessionRef } from "../contract/events.ts";
import { HASH_SEP, sessionKey } from "../contract/events.ts";
import {
  effectId,
  eventId,
  grantId,
  humanFactId,
  interactionId,
  intentId,
  payloadSha256,
  providerInvocationId,
  sessionId,
  stableJson,
} from "../contract/ids.ts";
import type {
  EffectState,
  EffectTerminalOutcome,
  EffectType,
  GrantStatus,
  HumanFactKind,
  InteractionState,
  OutboxState,
  ProviderRecoveryState,
  ProviderTerminalOutcome,
} from "../contract/lifecycle.ts";
import { AGENT_RUN_TERMINAL_STATES, isAgentRunTerminalState } from "../contract/lifecycle.ts";

export interface Clock {
  now(): Date;
}
export const systemClock: Clock = { now: () => new Date() };

/** A producer reused an idempotency key for materially different work. */
export class IdempotencyConflictError extends Error {
  readonly name = "IdempotencyConflictError";
  constructor(
    readonly scope: string,
    readonly key: string,
    readonly existingHash: string | null,
    readonly incomingHash: string,
    readonly objectType: string,
    readonly objectId: string,
  ) {
    super(`idempotency conflict for ${objectType} ${scope}/${key}`);
  }
}

/** A stale or non-owner worker attempted to mutate a leased row. */
export class StaleClaimError extends Error {
  readonly name = "StaleClaimError";
  constructor(readonly objectType: string, readonly objectId: string) {
    super(`stale or missing claim for ${objectType} ${objectId}`);
  }
}

export interface Claim {
  owner: string;
  token: string;
}

function sourceIdentity(source: CarEvent["source"]): string {
  return [source.vendor, source.host, source.adapter].join(HASH_SEP);
}

function claimToken(prefix: string): string {
  return intentId(`claim_${prefix}`);
}

function isExpired(value: string | null | undefined, now: string): boolean {
  return value !== null && value !== undefined && value < now;
}

function timestampBefore(candidate: string, existing: string): boolean {
  const candidateMs = Date.parse(candidate);
  const existingMs = Date.parse(existing);
  if (Number.isFinite(candidateMs) && Number.isFinite(existingMs)) return candidateMs < existingMs;
  return candidate < existing;
}

function asJson(value: unknown): string {
  return stableJson(value);
}

function digestReceiptAt(resultJson: string | null): string | null {
  if (!resultJson) return null;
  try {
    const parsed = JSON.parse(resultJson) as { receipt_at?: unknown };
    return typeof parsed.receipt_at === "string" ? parsed.receipt_at : null;
  } catch {
    return null;
  }
}

function parseHeldOutboxIds(value: string): number[] {
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((id): id is number => typeof id === "number" && Number.isInteger(id) && id > 0)
      : [];
  } catch {
    return [];
  }
}

function grantIdentityHash(input: GrantInput): string {
  return payloadSha256({
    intent_id: input.intentId,
    lineage_id: input.lineageId ?? null,
    scope: input.scope,
    effect_type: input.effectType,
    constraints: input.constraints ?? {},
    created_by: input.createdBy,
    expires_at: input.expiresAt ?? null,
    provenance: input.provenance ?? {},
  });
}

export function openDb(path: string): Database {
  const db = new Database(path, { create: true });
  db.exec("PRAGMA journal_mode = WAL;");
  db.exec("PRAGMA busy_timeout = 5000;");
  db.exec("PRAGMA foreign_keys = ON;");
  const current = (db.query("PRAGMA user_version").get() as { user_version: number }).user_version;
  for (let v = current; v < MIGRATIONS.length; v++) {
    db.transaction(() => {
      db.exec(MIGRATIONS[v]!);
      db.exec(`PRAGMA user_version = ${v + 1}`);
    })();
  }
  return db;
}

export type IngestResult =
  | { inserted: true; event_id: string; car_session_id: string | null }
  | { inserted: false; event_id: string; car_session_id: string | null };

export interface EventRow {
  id: string;
  idempotency_key: string;
  /** Optional in the TypeScript view for legacy fixture rows inserted directly. */
  source_id?: string;
  payload_sha256?: string | null;
  car_session_id: string | null;
  type: string;
  severity: string;
  ts: string;
  received_at: string;
  requires_response: number;
  response_channel_json: string | null;
  title: string;
  body: string;
  payload_json: string;
  expires_at: string | null;
  actor: string;
  triage_state: string;
  triage_lease_until?: string | null;
  triage_claim_owner?: string | null;
  triage_claim_token?: string | null;
  route_state?: string;
  route_claim_owner?: string | null;
  route_claim_token?: string | null;
  route_lease_until?: string | null;
  incident_id: string | null;
  source_vendor: string;
  source_host: string;
  source_adapter: string;
}

export interface ProviderInvocationRow {
  id: string;
  incident_id: string | null;
  provider_id: string;
  provider_instance: string;
  provider_version: string;
  capability: string;
  request_id: string;
  request_sha256: string;
  state: "pending" | "running" | "terminal_recorded";
  terminal_outcome: ProviderTerminalOutcome | null;
  recovery_state: ProviderRecoveryState | null;
  claim_owner: string | null;
  claim_token: string | null;
  lease_until: string | null;
  started_at: string | null;
  finished_at: string | null;
  response_ref: string | null;
  error_json: string | null;
  created_at: string;
}

export interface EffectRow {
  id: string;
  intent_id: string;
  decision_id: string | null;
  type: EffectType | string;
  args_json: string;
  args_sha256: string;
  lineage_id: string;
  scope_json?: string;
  lineage_json?: string | null;
  deadline_at?: string | null;
  action_class?: string | null;
  cost_usd?: number;
  provider_policy_verdict: string | null;
  safety_verdict: string;
  grant_id: string | null;
  dedupe_hash: string | null;
  state: EffectState;
  terminal_outcome: EffectTerminalOutcome | null;
  claim_owner: string | null;
  claim_token: string | null;
  lease_until: string | null;
  recovery_state: string | null;
  started_at: string | null;
  finished_at: string | null;
  result_json: string | null;
  created_at: string;
}

export interface ProviderInvocationInput {
  id?: string;
  incidentId?: string | null;
  providerId: string;
  providerInstance: string;
  providerVersion: string;
  capability: string;
  requestId: string;
  requestHash?: string;
}

export interface EffectInput {
  id?: string;
  intentId: string;
  decisionId?: string | null;
  type: EffectType | string;
  args: Record<string, unknown>;
  lineageId: string;
  providerPolicyVerdict?: string | null;
  safetyVerdict?: string;
  grantId?: string | null;
  dedupeHash?: string | null;
  state?: EffectState;
  scope?: Record<string, unknown>;
  lineage?: Record<string, unknown> | null;
  deadlineAt?: string | null;
  actionClass?: string | null;
  costUsd?: number;
  terminalOutcome?: EffectTerminalOutcome | null;
  result?: unknown;
  claimOwner?: string | null;
  claimToken?: string | null;
  leaseUntil?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

/**
 * Limits enforced by the same SQLite transaction that crosses an effect from
 * pending to running. Authorization is useful feedback, but claim is the
 * external-world authority boundary and therefore cannot trust an earlier
 * snapshot of these rolling rails.
 */
export interface EffectClaimSafetyLimits {
  maxAttemptsPerWindow: number;
  attemptWindowMs: number;
  maxFailuresPerWindow: number;
  failureWindowMs: number;
  spendWindowMs: number;
  maxSpendUsd: number;
  dedupeWindowMs: number;
  /** Exact immutable args hash when the core content rail rejected it. */
  dangerousArgsSha256: string | null;
}

export type EffectClaimBlockCode =
  | "panic"
  | "grant_required"
  | "grant_mismatch"
  | "grant_expired"
  | "grant_revoked"
  | "grant_consumed"
  | "dangerous_content"
  | "deadline_expired"
  | "dedupe"
  | "rate_limit"
  | "budget"
  | "circuit_breaker"
  | "conflict";

export type EffectClaimResult =
  | { claimed: true }
  | { claimed: false; code: EffectClaimBlockCode; blocked: boolean };

export interface InteractionInput {
  id?: string;
  sourceId: string;
  idempotencyKey: string;
  kind: HumanFactKind;
  targetType: string;
  targetId: string;
  actorId: string;
  body: Record<string, unknown>;
  lineageId?: string | null;
  expiresAt?: string | null;
  /** Optional grant created by the same human action transaction. */
  grant?: GrantInput;
}

export interface InteractionResult {
  inserted: boolean;
  interactionId: string;
  factId: string;
  grantId?: string;
}

export interface GrantInput {
  id?: string;
  intentId: string;
  lineageId?: string | null;
  scope: Record<string, unknown>;
  effectType: string;
  constraints?: Record<string, unknown>;
  usesRemaining?: number | null;
  createdBy: string;
  expiresAt?: string | null;
  provenance?: Record<string, unknown>;
  status?: GrantStatus;
  consumedAt?: string | null;
  revokedAt?: string | null;
}

export interface GrantRow {
  id: string;
  intent_id: string;
  lineage_id: string | null;
  scope_json: string;
  effect_type: string;
  constraint_json: string;
  status: GrantStatus;
  uses_remaining: number | null;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  consumed_at: string | null;
  revoked_at: string | null;
  provenance_json: string;
}

export interface PanicState {
  active: boolean;
  reason: string | null;
  changedAt: string | null;
}

export interface OutboxIntentInput {
  id?: number;
  intentId?: string;
  channel: string;
  target: unknown;
  body: unknown;
  route?: unknown;
  remoteIdempotencyKey?: string | null;
  nextAttemptAt?: string;
}

export interface OutboxRow {
  id: number;
  intent_id: string | null;
  channel: string;
  target_json: string;
  body_json: string;
  state: OutboxState | string;
  route_json: string;
  claim_owner: string | null;
  claim_token: string | null;
  lease_until: string | null;
  attempts: number;
  next_attempt_at: string;
  remote_idempotency_key: string | null;
  recovery_state: string | null;
  result_json: string | null;
  sent_message_id: string | null;
  created_at: string;
}

export interface DigestRow {
  day: string;
  rendered_md: string;
  sent_at: string | null;
  outbox_id: number | null;
  held_outbox_ids_json: string;
}

export interface AgentRunInput {
  executionId: string;
  transport?: string;
  agent: string;
  authority?: string | null;
  mode?: string | null;
  state: string;
  liveness?: string | null;
  labels?: string[];
  title?: string | null;
  repo?: string | null;
  cwd?: string | null;
  profile?: string | null;
  model?: string | null;
  runtime?: string | null;
  continuationSupported?: boolean;
  startedAt?: string | null;
  updatedAt: string;
  terminalAt?: string | null;
  durationSeconds?: number | null;
  statusRevision?: number;
  observationState?: "observed" | "reconciled" | "stale" | "unknown";
  lastMeaningfulUpdate?: string | null;
  raw?: Record<string, unknown>;
}

export interface AgentRunRow {
  execution_id: string;
  transport: string;
  agent: string;
  authority: string | null;
  mode: string | null;
  state: string;
  liveness: string | null;
  labels_json: string;
  title: string | null;
  repo: string | null;
  cwd: string | null;
  profile: string | null;
  model: string | null;
  runtime: string | null;
  continuation_supported: number;
  started_at: string | null;
  updated_at: string;
  terminal_at: string | null;
  duration_seconds: number | null;
  status_revision: number;
  observation_state: string;
  last_meaningful_update: string | null;
  raw_json: string;
  first_observed_at: string;
  last_observed_at: string;
}

export class Store {
  constructor(
    readonly db: Database,
    readonly clock: Clock = systemClock,
  ) {}

  audit(actor: string, verb: string, objectType: string, objectId: string, detail: unknown = {}): void {
    this.db
      .query(
        "INSERT INTO audit (ts, actor, verb, object_type, object_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
      )
      .run(this.clock.now().toISOString(), actor, verb, objectType, objectId, JSON.stringify(detail));
  }

  /**
   * Upsert the compact agent-run projection. Poll timestamps refresh on every
   * observation, while audit evidence is emitted only for creation or a
   * semantic state change so watchdog polling cannot flood the journal.
   */
  upsertAgentRun(input: AgentRunInput): AgentRunRow {
    const observedAt = this.clock.now().toISOString();
    return this.db.transaction(() => {
      const existing = this.db.query("SELECT * FROM agent_runs WHERE execution_id = ?").get(input.executionId) as AgentRunRow | null;
      const labelsJson = JSON.stringify(input.labels ?? []);
      const rawJson = JSON.stringify(input.raw ?? {});
      const transport = input.transport ?? "agentctl";
      const observationState = input.observationState ?? "observed";
      const continuationSupported = input.continuationSupported ? 1 : 0;
      if (!existing) {
        this.db.query(
          `INSERT INTO agent_runs (
             execution_id, transport, agent, authority, mode, state, liveness,
             labels_json, title, repo, cwd, profile, model, runtime,
             continuation_supported, started_at, updated_at, terminal_at,
             duration_seconds, status_revision, observation_state,
             last_meaningful_update, raw_json, first_observed_at, last_observed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        ).run(
          input.executionId, transport, input.agent, input.authority ?? null,
          input.mode ?? null, input.state, input.liveness ?? null, labelsJson,
          input.title ?? null, input.repo ?? null, input.cwd ?? null,
          input.profile ?? null, input.model ?? null, input.runtime ?? null,
          continuationSupported, input.startedAt ?? null, input.updatedAt,
          input.terminalAt ?? null, input.durationSeconds ?? null,
          input.statusRevision ?? 0, observationState, input.lastMeaningfulUpdate ?? input.updatedAt,
          rawJson, observedAt, observedAt,
        );
        this.audit("daemon", "agent_run.observed", "agent_run", input.executionId, {
          agent: input.agent,
          state: input.state,
          observation_state: observationState,
        });
      } else {
        // A delayed snapshot may refresh observation health, but must never
        // overwrite a newer semantic state.
        const terminalRegression = isAgentRunTerminalState(existing.state) && !isAgentRunTerminalState(input.state);
        if ((input.statusRevision !== undefined && input.statusRevision < existing.status_revision) ||
          timestampBefore(input.updatedAt, existing.updated_at) || terminalRegression) {
          this.db.query("UPDATE agent_runs SET last_observed_at = ? WHERE execution_id = ?")
            .run(observedAt, input.executionId);
          return this.db.query("SELECT * FROM agent_runs WHERE execution_id = ?").get(input.executionId) as AgentRunRow;
        }
        const changed = existing.agent !== input.agent || existing.state !== input.state ||
          existing.liveness !== (input.liveness ?? null) ||
          existing.observation_state !== observationState ||
          existing.terminal_at !== (input.terminalAt ?? null);
        this.db.query(
          `UPDATE agent_runs SET transport = ?, agent = ?, authority = ?, mode = ?,
             state = ?, liveness = ?, labels_json = ?, title = COALESCE(?, title),
             repo = COALESCE(?, repo), cwd = COALESCE(?, cwd),
             profile = COALESCE(?, profile), model = COALESCE(?, model),
             runtime = COALESCE(?, runtime), continuation_supported = ?,
             started_at = COALESCE(?, started_at), updated_at = ?,
             terminal_at = COALESCE(?, terminal_at),
             duration_seconds = COALESCE(?, duration_seconds), status_revision = ?,
             observation_state = ?, last_meaningful_update = ?, raw_json = ?,
             last_observed_at = ? WHERE execution_id = ?`,
        ).run(
          transport, input.agent, input.authority ?? existing.authority,
          input.mode ?? existing.mode, input.state, input.liveness ?? null,
          labelsJson, input.title ?? null, input.repo ?? null, input.cwd ?? null,
          input.profile ?? null, input.model ?? null, input.runtime ?? null,
          continuationSupported, input.startedAt ?? null, input.updatedAt,
          input.terminalAt ?? null, input.durationSeconds ?? null,
          input.statusRevision ?? existing.status_revision, observationState,
          changed ? (input.lastMeaningfulUpdate ?? input.updatedAt) : existing.last_meaningful_update,
          rawJson, observedAt, input.executionId,
        );
        if (changed) {
          this.audit("daemon", "agent_run.changed", "agent_run", input.executionId, {
            from: { agent: existing.agent, state: existing.state, liveness: existing.liveness, observation_state: existing.observation_state },
            to: { agent: input.agent, state: input.state, liveness: input.liveness ?? null, observation_state: observationState },
          });
        }
      }
      return this.db.query("SELECT * FROM agent_runs WHERE execution_id = ?").get(input.executionId) as AgentRunRow;
    })();
  }

  getAgentRun(executionId: string): AgentRunRow | null {
    return (this.db.query("SELECT * FROM agent_runs WHERE execution_id = ?").get(executionId) as AgentRunRow | null) ?? null;
  }

  /**
   * A successful, complete nonterminal poll is negative evidence too. Runs
   * that were previously active but vanished from that authoritative bounded
   * view become last-seen/unknown instead of remaining falsely "running".
   */
  markMissingAgentRunsUnknown(seenActiveExecutionIds: string[]): number {
    const terminalPlaceholders = AGENT_RUN_TERMINAL_STATES.map(() => "?").join(",");
    const seenClause = seenActiveExecutionIds.length
      ? `AND execution_id NOT IN (${seenActiveExecutionIds.map(() => "?").join(",")})`
      : "";
    const rows = this.db.query(
      `SELECT execution_id, state, liveness, last_observed_at
       FROM agent_runs
       WHERE transport = 'agentctl'
         AND terminal_at IS NULL
         AND lower(state) NOT IN (${terminalPlaceholders})
         AND observation_state != 'unknown'
         ${seenClause}`,
    ).all(...AGENT_RUN_TERMINAL_STATES, ...seenActiveExecutionIds) as {
      execution_id: string;
      state: string;
      liveness: string | null;
      last_observed_at: string;
    }[];
    if (rows.length === 0) return 0;
    const now = this.clock.now().toISOString();
    this.db.transaction(() => {
      for (const row of rows) {
        this.db.query(
          "UPDATE agent_runs SET observation_state = 'unknown', last_meaningful_update = ? WHERE execution_id = ?",
        ).run(now, row.execution_id);
        this.audit("daemon", "agent_run.missing", "agent_run", row.execution_id, {
          state: row.state,
          liveness: row.liveness,
          last_observed_at: row.last_observed_at,
        });
      }
    })();
    return rows.length;
  }

  /** Bound only terminal projection rows; unresolved work is never selected. */
  pruneAgentRuns(retentionDays: number, maxTerminal: number): { age: number; overflow: number } {
    const terminalPlaceholders = AGENT_RUN_TERMINAL_STATES.map(() => "?").join(",");
    const terminalWhere = `(terminal_at IS NOT NULL OR lower(state) IN (${terminalPlaceholders}))`;
    const cutoff = new Date(this.clock.now().getTime() - retentionDays * 86_400_000).toISOString();
    return this.db.transaction(() => {
      const aged = this.db.query(
        `SELECT execution_id FROM agent_runs
         WHERE ${terminalWhere} AND COALESCE(terminal_at, updated_at) < ?`,
      ).all(...AGENT_RUN_TERMINAL_STATES, cutoff) as { execution_id: string }[];
      for (const row of aged) {
        this.db.query("DELETE FROM agent_runs WHERE execution_id = ?").run(row.execution_id);
      }

      const overflow = this.db.query(
        `SELECT execution_id FROM agent_runs
         WHERE ${terminalWhere}
         ORDER BY COALESCE(terminal_at, updated_at) DESC, execution_id DESC
         LIMIT -1 OFFSET ?`,
      ).all(...AGENT_RUN_TERMINAL_STATES, maxTerminal) as { execution_id: string }[];
      for (const row of overflow) {
        this.db.query("DELETE FROM agent_runs WHERE execution_id = ?").run(row.execution_id);
      }
      if (aged.length || overflow.length) {
        this.audit("daemon", "agent_run.pruned", "projection", "agent_runs", {
          age: aged.length,
          overflow: overflow.length,
          retention_days: retentionDays,
          max_terminal: maxTerminal,
        });
      }
      return { age: aged.length, overflow: overflow.length };
    })();
  }

  /**
   * Idempotent ingest: resolves/creates the session, inserts the event unless the
   * idempotency key already exists. ACK only after this commits.
   */
  ingestEvent(
    ev: CarEvent,
    opts: { actor?: "external" | "car"; sourceId?: string; verifiedRepo?: string | null } = {},
  ): IngestResult {
    const now = this.clock.now().toISOString();
    return this.db.transaction((): IngestResult => {
      const sourceId = opts.sourceId ?? sourceIdentity(ev.source);
      const hash = payloadSha256(ev);
      const id = eventId();
      const reservation = this.reserveIdentity(sourceId, ev.idempotency_key, hash, "event", id, now);
      if (!reservation.inserted) {
        const existing = this.db.query("SELECT car_session_id FROM events WHERE id = ?").get(reservation.objectId) as
          | { car_session_id: string | null }
          | null;
        return { inserted: false, event_id: reservation.objectId, car_session_id: existing?.car_session_id ?? null };
      }
      // Public wire data may carry a display repo label, but only an
      // adapter/core resolver may supply authorization-grade identity.
      const sessionRef = ev.session
        ? {
            ...ev.session,
            ...(opts.verifiedRepo ? { repo: opts.verifiedRepo } : {}),
            repo_verified: Boolean(opts.verifiedRepo),
          }
        : null;
      const carSessionId = sessionRef ? this.upsertSession(sessionRef, ev.ts) : null;
      this.db
        .query(
          `INSERT INTO events (id, idempotency_key, source_id, payload_sha256, car_session_id, type, severity, ts, received_at,
             requires_response, response_channel_json, title, body, payload_json, expires_at, actor,
             source_vendor, source_host, source_adapter)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .run(
          id,
          ev.idempotency_key,
          sourceId,
          hash,
          carSessionId,
          ev.type,
          ev.severity,
          ev.ts,
          now,
          ev.requires_response ? 1 : 0,
          ev.response_channel ? JSON.stringify(ev.response_channel) : null,
          ev.title,
          ev.body,
          JSON.stringify(ev.payload),
          ev.expires_at ?? null,
          opts.actor ?? "external",
          ev.source.vendor,
          ev.source.host,
          ev.source.adapter,
        );
      if (ev.type === "heartbeat" && carSessionId) {
        this.db
          .query("UPDATE sessions SET last_heartbeat_at = ? WHERE car_session_id = ?")
          .run(ev.ts, carSessionId);
      }
      this.audit("daemon", "event.ingested", "event", id, {
        type: ev.type,
        source: ev.source,
        session: carSessionId,
      });
      return { inserted: true, event_id: id, car_session_id: carSessionId };
    })();
  }

  /** Resolve or create the canonical session for a ref; updates last_event_at. */
  upsertSession(ref: Omit<SessionRef, "repo_verified"> & { repo_verified?: boolean }, eventTs: string): string {
    const found = this.db
      .query("SELECT car_session_id FROM session_refs WHERE vendor = ? AND host = ? AND native_id = ?")
      .get(ref.vendor, ref.host, ref.native_id) as { car_session_id: string } | null;
    if (found) {
      this.db
        .query(
          `UPDATE sessions SET last_event_at = ?,
             title = COALESCE(?, title), cwd = COALESCE(?, cwd),
             repo = CASE WHEN repo_verified = 1 AND ? = 0 THEN repo ELSE COALESCE(?, repo) END,
             repo_verified = CASE WHEN ? = 1 THEN 1 ELSE repo_verified END
           WHERE car_session_id = ?`,
        )
        .run(
          eventTs,
          ref.title ?? null,
          ref.cwd ?? null,
          ref.repo_verified ? 1 : 0,
          ref.repo ?? null,
          ref.repo_verified ? 1 : 0,
          found.car_session_id,
        );
      return found.car_session_id;
    }
    const id = sessionId();
    this.db
      .query(
        `INSERT INTO sessions (car_session_id, vendor, host, title, cwd, repo, repo_verified, first_seen, last_event_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(id, ref.vendor, ref.host, ref.title ?? null, ref.cwd ?? null, ref.repo ?? null, ref.repo_verified ? 1 : 0, eventTs, eventTs);
    this.db
      .query("INSERT INTO session_refs (vendor, host, native_id, car_session_id) VALUES (?, ?, ?, ?)")
      .run(ref.vendor, ref.host, ref.native_id, id);
    this.audit("daemon", "session.created", "session", id, { key: sessionKey(ref) });
    return id;
  }

  /** Link an additional native ref (e.g. codex uuid inside an agentctl exec) to an existing session. */
  linkSessionRef(carSessionId: string, ref: Pick<SessionRef, "vendor" | "host" | "native_id">): void {
    this.db
      .query(
        "INSERT OR IGNORE INTO session_refs (vendor, host, native_id, car_session_id) VALUES (?, ?, ?, ?)",
      )
      .run(ref.vendor, ref.host, ref.native_id, carSessionId);
    this.audit("daemon", "session.ref_linked", "session", carSessionId, ref);
  }

  /**
   * Claim up to `limit` pending events for triage with a lease; expired leases
   * (crashed worker) are reclaimed. Returns claimed rows.
   */
  claimPendingEvents(limit: number, leaseSeconds: number, owner = "router"): EventRow[] {
    return this.claimEvents(limit, leaseSeconds, owner);
  }

  /** Claim events with an explicit owner and opaque fencing token. */
  claimEvents(limit: number, leaseSeconds: number, owner: string): EventRow[] {
    const now = this.clock.now();
    const leaseUntil = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    return this.db.transaction((): EventRow[] => {
      const rows = this.db
        .query(
          `SELECT * FROM events
           WHERE (triage_state = 'pending')
              OR (triage_state = 'coalescing' AND
                  (COALESCE(route_lease_until, triage_lease_until) IS NULL OR
                   COALESCE(route_lease_until, triage_lease_until) < ?))
           ORDER BY received_at ASC LIMIT ?`,
        )
        .all(now.toISOString(), limit) as EventRow[];
      const claimed: EventRow[] = [];
      for (const row of rows) {
        const token = claimToken("event");
        const wasExpired = row.triage_state === "coalescing";
        const changed = this.db
          .query(
            `UPDATE events SET triage_state = 'coalescing', triage_lease_until = ?,
               triage_claim_owner = ?, triage_claim_token = ?, route_state = 'pending',
               route_claim_owner = ?, route_claim_token = ?, route_lease_until = ? WHERE id = ?
               AND (triage_state = 'pending' OR (triage_state = 'coalescing' AND
                    (COALESCE(route_lease_until, triage_lease_until) IS NULL OR
                     COALESCE(route_lease_until, triage_lease_until) < ?)))`,
          )
          .run(leaseUntil, owner, token, owner, token, leaseUntil, row.id, now.toISOString());
        if (changed.changes === 0) continue;
        row.triage_state = "coalescing";
        row.triage_lease_until = leaseUntil;
        row.triage_claim_owner = owner;
        row.triage_claim_token = token;
        row.route_claim_owner = owner;
        row.route_claim_token = token;
        row.route_lease_until = leaseUntil;
        claimed.push(row);
        if (wasExpired) {
          this.audit("daemon", "event.reclaimed", "event", row.id, { owner, claim_token: token });
        }
      }
      return claimed;
    })();
  }

  renewEventLease(id: string, claim: Claim, leaseSeconds: number): boolean {
    const now = this.clock.now().toISOString();
    const until = new Date(this.clock.now().getTime() + leaseSeconds * 1000).toISOString();
    const result = this.db
      .query(
        `UPDATE events SET triage_lease_until = ?, route_lease_until = ?
         WHERE id = ? AND triage_claim_owner = ? AND triage_claim_token = ?
           AND COALESCE(route_lease_until, triage_lease_until) >= ?`,
      )
      .run(until, until, id, claim.owner, claim.token, now);
    return result.changes > 0;
  }

  /**
   * Compatibility callers may still transition by id. New workers must pass a
   * claim; token-checked transitions fence stale workers after a reclaim.
   */
  setEventTriageState(id: string, state: string, incidentId?: string, claim?: Claim): void {
    const result = claim
      ? this.db
          .query(
            `UPDATE events SET triage_state = ?, route_state = ?, incident_id = COALESCE(?, incident_id),
               triage_claim_owner = NULL, triage_claim_token = NULL, triage_lease_until = NULL,
               route_claim_owner = NULL, route_claim_token = NULL, route_lease_until = NULL WHERE id = ?
               AND triage_claim_owner = ? AND triage_claim_token = ?
               AND COALESCE(route_lease_until, triage_lease_until) >= ?`,
          )
          .run(state, state, incidentId ?? null, id, claim.owner, claim.token, this.clock.now().toISOString())
      : this.db
          .query("UPDATE events SET triage_state = ?, route_state = ?, incident_id = COALESCE(?, incident_id) WHERE id = ?")
          .run(state, state, incidentId ?? null, id);
    if (claim && result.changes === 0) throw new StaleClaimError("event", id);
    this.audit("daemon", "event.state", "event", id, { state, incident_id: incidentId ?? null });
  }

  completeEventClaim(id: string, claim: Claim, state: string, incidentId?: string): void {
    this.setEventTriageState(id, state, incidentId, claim);
  }

  enqueueOutbox(channel: string, target: unknown, body: unknown): number {
    return this.enqueueOutboxIntent({ channel, target, body }).outboxId;
  }

  /** Persist the archive row before its delivery side effect is enqueued. */
  recordDigest(day: string, renderedMd: string, heldOutboxIds: number[] = []): void {
    const held = heldOutboxIds.filter((id) => Number.isInteger(id) && id > 0);
    this.db
      .query(
        `INSERT INTO digests (day, rendered_md, sent_at, outbox_id, held_outbox_ids_json)
         VALUES (?, ?, NULL, NULL, ?)
         ON CONFLICT(day) DO UPDATE SET
           rendered_md = CASE WHEN digests.sent_at IS NULL THEN excluded.rendered_md ELSE digests.rendered_md END,
           held_outbox_ids_json = CASE WHEN digests.sent_at IS NULL THEN excluded.held_outbox_ids_json ELSE digests.held_outbox_ids_json END`,
      )
      .run(day, renderedMd, asJson(held));
    this.audit("daemon", "digest.recorded", "digest", day, { held_outbox_ids: held });
  }

  getDigest(day: string): DigestRow | null {
    return (this.db.query("SELECT * FROM digests WHERE day = ?").get(day) as DigestRow | null) ?? null;
  }

  /** Link the digest archive to its deterministic canonical delivery intent. */
  attachDigestOutbox(day: string, outboxId: number): boolean {
    const result = this.db
      .query("UPDATE digests SET outbox_id = COALESCE(outbox_id, ?) WHERE day = ?")
      .run(outboxId, day);
    if (result.changes > 0) {
      this.audit("daemon", "digest.outbox_linked", "digest", day, { outbox_id: outboxId });
      return true;
    }
    return Boolean(this.db.query("SELECT day FROM digests WHERE day = ? AND outbox_id = ?").get(day, outboxId));
  }

  findOutboxByIntent(intentId: string, channel = "telegram"): OutboxRow | null {
    return (
      (this.db
        .query("SELECT * FROM outbox WHERE intent_id = ? AND channel = ?")
        .get(intentId, channel) as OutboxRow | null) ?? null
    );
  }

  /**
   * Apply digest delivery side effects after the canonical outbox receipt is
   * durable. This is safe to replay after a crash between those two writes.
   */
  reconcileDigestReceipts(): number {
    const now = this.clock.now().toISOString();
    return this.db.transaction(() => {
      const rows = this.db
        .query(
          `SELECT d.day, d.held_outbox_ids_json, o.id AS outbox_id, o.result_json
           FROM digests d JOIN outbox o ON o.id = d.outbox_id
           WHERE d.sent_at IS NULL AND o.state = 'delivered'`,
        )
        .all() as { day: string; held_outbox_ids_json: string; outbox_id: number; result_json: string | null }[];
      for (const row of rows) {
        const receiptAt = digestReceiptAt(row.result_json) ?? now;
        const updated = this.db
          .query("UPDATE digests SET sent_at = ? WHERE day = ? AND sent_at IS NULL")
          .run(receiptAt, row.day);
        if (updated.changes === 0) continue;
        const heldIds = parseHeldOutboxIds(row.held_outbox_ids_json);
        for (const heldId of heldIds) {
          const held = this.db
            .query("UPDATE outbox SET state = 'delivered' WHERE id = ? AND state = 'deferred'")
            .run(heldId);
          if (held.changes > 0) {
            this.audit("daemon", "outbox.folded_into_digest", "outbox", String(heldId), {
              digest_day: row.day,
              digest_outbox_id: row.outbox_id,
            });
          }
        }
        this.db
          .query(
            `INSERT INTO kv (key, value_json, updated_at) VALUES ('scheduler.last_digest_day', ?, ?)
             ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at`,
          )
          .run(asJson(row.day), now);
        this.audit("daemon", "digest.delivered", "digest", row.day, {
          outbox_id: row.outbox_id,
          receipt_at: receiptAt,
          held_outbox_ids: heldIds,
        });
      }
      return rows.length;
    })();
  }

  /**
   * Atomically reserve a producer key. INSERT OR IGNORE handles the cross-process
   * race; the follow-up read distinguishes a replay from a same-key conflict.
   */
  private reserveIdentity(
    scope: string,
    key: string,
    hash: string,
    objectType: string,
    objectId: string,
    now = this.clock.now().toISOString(),
  ): { inserted: boolean; objectId: string } {
    const insert = this.db
      .query(
        `INSERT OR IGNORE INTO idempotency (scope, key, payload_sha256, object_type, object_id, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(scope, key, hash, objectType, objectId, now);
    const row = this.db
      .query("SELECT payload_sha256, object_type, object_id FROM idempotency WHERE scope = ? AND key = ?")
      .get(scope, key) as { payload_sha256: string | null; object_type: string; object_id: string } | null;
    if (!row) throw new Error(`idempotency reservation disappeared for ${scope}/${key}`);
    if (row.object_type !== objectType) {
      throw new IdempotencyConflictError(scope, key, row.payload_sha256, hash, row.object_type, row.object_id);
    }
    // Rows imported from v2 have no canonical hash. The first v3 replay binds
    // the legacy row to its canonical representation; later replays are checked.
    if (row.payload_sha256 === null) {
      this.db
        .query("UPDATE idempotency SET payload_sha256 = ? WHERE scope = ? AND key = ? AND payload_sha256 IS NULL")
        .run(hash, scope, key);
      return { inserted: false, objectId: row.object_id };
    }
    if (row.payload_sha256 !== hash) {
      throw new IdempotencyConflictError(scope, key, row.payload_sha256, hash, row.object_type, row.object_id);
    }
    return { inserted: insert.changes > 0, objectId: row.object_id };
  }

  /** Public status-oriented form for producers that do not create a row here. */
  reserveIdempotency(
    scope: string,
    key: string,
    fingerprint: string,
    objectType = "generic",
    objectId = key,
  ): "inserted" | "duplicate" | "conflict" {
    try {
      return this.reserveIdentity(scope, key, fingerprint, objectType, objectId).inserted ? "inserted" : "duplicate";
    } catch (error) {
      if (error instanceof IdempotencyConflictError) return "conflict";
      throw error;
    }
  }

  enqueueOutboxIntent(input: OutboxIntentInput): { inserted: boolean; outboxId: number; intentId: string } {
    const now = this.clock.now().toISOString();
    const resolvedIntentId = input.intentId ?? intentId("intent");
    const route = input.route ?? {};
    const hash = payloadSha256({
      channel: input.channel,
      target: input.target,
      body: input.body,
      route,
      remote_idempotency_key: input.remoteIdempotencyKey ?? null,
    });
    return this.db.transaction(() => {
      const reservation = this.reserveIdentity(
        "outbox",
        `${resolvedIntentId}:${input.channel}`,
        hash,
        "outbox",
        resolvedIntentId,
        now,
      );
      if (!reservation.inserted) {
        const existing = this.db
          .query("SELECT id, intent_id FROM outbox WHERE intent_id = ? AND channel = ?")
          .get(resolvedIntentId, input.channel) as { id: number; intent_id: string } | null;
        if (!existing) throw new Error(`outbox reservation has no row for ${resolvedIntentId}`);
        return { inserted: false, outboxId: existing.id, intentId: resolvedIntentId };
      }
      const nextAttemptAt = input.nextAttemptAt ?? now;
      const result = this.db
        .query(
          `INSERT INTO outbox (intent_id, channel, target_json, body_json, state, route_json,
             attempts, next_attempt_at, remote_idempotency_key, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?)`,
        )
        .run(
          resolvedIntentId,
          input.channel,
          asJson(input.target),
          asJson(input.body),
          asJson(route),
          nextAttemptAt,
          input.remoteIdempotencyKey ?? null,
          now,
        );
      const outboxId = Number(result.lastInsertRowid);
      this.audit("daemon", "outbox.enqueued", "outbox", String(outboxId), {
        intent_id: resolvedIntentId,
        channel: input.channel,
      });
      return { inserted: true, outboxId, intentId: resolvedIntentId };
    })();
  }

  getEvent(id: string): EventRow | null {
    return (this.db.query("SELECT * FROM events WHERE id = ?").get(id) as EventRow | null) ?? null;
  }

  /** Reclaim expired event claims and expose the recovery as an audit fact. */
  recoverExpiredEventClaims(): number {
    const now = this.clock.now().toISOString();
    const rows = this.db
      .query(
        `SELECT id, triage_claim_owner, triage_claim_token FROM events
         WHERE triage_state = 'coalescing' AND
           (COALESCE(route_lease_until, triage_lease_until) IS NULL OR
            COALESCE(route_lease_until, triage_lease_until) < ?)`,
      )
      .all(now) as { id: string; triage_claim_owner: string | null; triage_claim_token: string | null }[];
    for (const row of rows) {
      this.audit("daemon", "event.lease_expired", "event", row.id, {
        previous_owner: row.triage_claim_owner,
        previous_token: row.triage_claim_token,
      });
    }
    return rows.length;
  }

  createProviderInvocation(input: ProviderInvocationInput): { inserted: boolean; id: string } {
    const now = this.clock.now().toISOString();
    const id = input.id ?? providerInvocationId();
    const requestHash = input.requestHash ?? payloadSha256({
      incident_id: input.incidentId ?? null,
      provider_id: input.providerId,
      provider_instance: input.providerInstance,
      provider_version: input.providerVersion,
      capability: input.capability,
      request_id: input.requestId,
    });
    return this.db.transaction(() => {
      const reservation = this.reserveIdentity(
        `provider:${input.providerInstance}`,
        input.requestId,
        requestHash,
        "provider_invocation",
        id,
        now,
      );
      if (!reservation.inserted) return { inserted: false, id: reservation.objectId };
      this.db
        .query(
          `INSERT INTO provider_invocations
           (id, incident_id, provider_id, provider_instance, provider_version, capability,
            request_id, request_sha256, state, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)`,
        )
        .run(
          id,
          input.incidentId ?? null,
          input.providerId,
          input.providerInstance,
          input.providerVersion,
          input.capability,
          input.requestId,
          requestHash,
          now,
        );
      this.audit("daemon", "provider_invocation.created", "provider_invocation", id, {
        provider_id: input.providerId,
        provider_instance: input.providerInstance,
        capability: input.capability,
        request_id: input.requestId,
      });
      return { inserted: true, id };
    })();
  }

  getProviderInvocation(id: string): ProviderInvocationRow | null {
    return (
      (this.db.query("SELECT * FROM provider_invocations WHERE id = ?").get(id) as ProviderInvocationRow | null) ??
      null
    );
  }

  claimProviderInvocation(id: string, owner = "provider", leaseSeconds = 120): ProviderInvocationRow | null {
    const now = this.clock.now();
    const nowIso = now.toISOString();
    const until = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    return this.db.transaction(() => {
      const row = this.getProviderInvocation(id);
      if (!row || row.state === "terminal_recorded") return null;
      const expired = row.state === "running" && isExpired(row.lease_until, nowIso);
      if (row.state !== "pending" && !expired) return null;
      const token = claimToken("provider");
      const result = this.db
        .query(
          `UPDATE provider_invocations SET state = 'running', claim_owner = ?, claim_token = ?,
             lease_until = ?, started_at = COALESCE(started_at, ?),
             recovery_state = CASE WHEN ? THEN 'reclaimed' ELSE recovery_state END
           WHERE id = ? AND (state = 'pending' OR (state = 'running' AND lease_until < ?))`,
        )
        .run(owner, token, until, nowIso, expired ? 1 : 0, id, nowIso);
      if (result.changes === 0) return null;
      if (expired) this.audit("daemon", "provider_invocation.reclaimed", "provider_invocation", id, { owner });
      return this.getProviderInvocation(id);
    })();
  }

  claimPendingProviderInvocations(limit: number, owner = "provider", leaseSeconds = 120): ProviderInvocationRow[] {
    const ids = this.db
      .query(
        `SELECT id FROM provider_invocations
         WHERE state = 'pending' OR (state = 'running' AND lease_until < ?)
         ORDER BY created_at ASC LIMIT ?`,
      )
      .all(this.clock.now().toISOString(), limit) as { id: string }[];
    return ids.flatMap(({ id }) => {
      const row = this.claimProviderInvocation(id, owner, leaseSeconds);
      return row ? [row] : [];
    });
  }

  renewProviderInvocationLease(id: string, claim: Claim, leaseSeconds: number): boolean {
    const until = new Date(this.clock.now().getTime() + leaseSeconds * 1000).toISOString();
    const result = this.db
      .query(
         `UPDATE provider_invocations SET lease_until = ? WHERE id = ? AND state = 'running'
         AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
      )
      .run(until, id, claim.owner, claim.token, this.clock.now().toISOString());
    return result.changes > 0;
  }

  recordProviderTerminal(
    id: string,
    claim: Claim,
    outcome: ProviderTerminalOutcome,
    result: { responseRef?: string | null; error?: unknown } = {},
  ): void {
    const now = this.clock.now().toISOString();
    const existing = this.getProviderInvocation(id);
    if (!existing) throw new Error(`unknown provider invocation ${id}`);
    if (existing.state === "terminal_recorded") {
      if (existing.terminal_outcome !== outcome) {
        throw new IdempotencyConflictError("provider_invocation", id, existing.terminal_outcome, outcome, "terminal", id);
      }
      return;
    }
    const updated = this.db
      .query(
        `UPDATE provider_invocations SET state = 'terminal_recorded', terminal_outcome = ?,
         finished_at = ?, response_ref = ?, error_json = ?, claim_owner = NULL,
           claim_token = NULL, lease_until = NULL
         WHERE id = ? AND state = 'running' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
      )
      .run(
        outcome,
        now,
        result.responseRef ?? null,
        result.error === undefined ? null : asJson(result.error),
        id,
        claim.owner,
        claim.token,
        now,
      );
    if (updated.changes === 0) throw new StaleClaimError("provider_invocation", id);
    this.audit("daemon", "provider_invocation.terminal_recorded", "provider_invocation", id, { outcome });
  }

  createEffect(input: EffectInput): { inserted: boolean; id: string } {
    const now = this.clock.now().toISOString();
    const id = input.id ?? effectId();
    const argsJson = asJson(input.args);
    const argsHash = payloadSha256(input.args);
    const scopeJson = asJson(input.scope ?? {});
    const lineageJson = input.lineage === undefined || input.lineage === null ? null : asJson(input.lineage);
    const compatibilityLineage = lineageJson ?? input.lineageId;
    return this.db.transaction(() => {
      const reservation = this.reserveIdentity("effect", input.intentId, payloadSha256({
        type: input.type,
        args: input.args,
        lineage_id: input.lineageId,
        lineage: input.lineage ?? null,
        scope: input.scope ?? {},
        decision_id: input.decisionId ?? null,
      }), "effect", id, now);
      if (!reservation.inserted) return { inserted: false, id: reservation.objectId };
      this.db
        .query(
          `INSERT INTO effects
           (id, intent_id, decision_id, type, args_json, args_sha256, lineage_id,
            scope_json, lineage_json, deadline_at, action_class, cost_usd,
            provider_policy_verdict, safety_verdict, grant_id, dedupe_hash, state,
            terminal_outcome, claim_owner, claim_token, lease_until, started_at,
            finished_at, result_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .run(
          id,
          input.intentId,
          input.decisionId ?? null,
          input.type,
          argsJson,
          argsHash,
          compatibilityLineage,
          scopeJson,
          lineageJson,
          input.deadlineAt ?? null,
          input.actionClass ?? null,
          input.costUsd ?? 0,
          input.providerPolicyVerdict ?? null,
          input.safetyVerdict ?? "pending",
          input.grantId ?? null,
          input.dedupeHash ?? null,
          input.state ?? "proposed",
          input.terminalOutcome ?? null,
          input.claimOwner ?? null,
          input.claimToken ?? null,
          input.leaseUntil ?? null,
          input.startedAt ?? null,
          input.finishedAt ?? null,
          input.result === undefined ? null : asJson(input.result),
          now,
        );
      this.audit("daemon", "effect.proposed", "effect", id, { intent_id: input.intentId, type: input.type });
      return { inserted: true, id };
    })();
  }

  getEffect(id: string): EffectRow | null {
    return (
      (this.db.query("SELECT * FROM effects WHERE id = ? OR intent_id = ? LIMIT 1").get(id, id) as EffectRow | null) ??
      null
    );
  }

  getEffectByIntent(intentIdValue: string): EffectRow | null {
    return (this.db.query("SELECT * FROM effects WHERE intent_id = ?").get(intentIdValue) as EffectRow | null) ?? null;
  }

  listEffects(): EffectRow[] {
    return this.db.query("SELECT * FROM effects ORDER BY created_at ASC, id ASC").all() as EffectRow[];
  }

  authorizeEffect(
    intentIdValue: string,
    grantIdValue: string | null,
    safetyVerdict = "allowed",
    providerPolicyVerdict?: string | null,
  ): EffectRow {
    const current = this.getEffectByIntent(intentIdValue);
    if (!current) throw new Error(`unknown effect intent ${intentIdValue}`);
    if (current.state === "terminal_recorded") return current;
    this.db
      .query(
        `UPDATE effects SET state = 'pending', grant_id = ?, safety_verdict = ?,
           provider_policy_verdict = COALESCE(?, provider_policy_verdict) WHERE intent_id = ?
           AND state IN ('proposed', 'blocked', 'pending')`,
      )
      .run(grantIdValue, safetyVerdict, providerPolicyVerdict ?? null, intentIdValue);
    this.audit("daemon", "effect.authorized", "effect", current.id, {
      intent_id: intentIdValue,
      grant_id: grantIdValue,
    });
    return this.getEffectByIntent(intentIdValue)!;
  }

  blockEffectByIntent(intentIdValue: string, safetyVerdict: string, reason?: unknown): EffectRow {
    const current = this.getEffectByIntent(intentIdValue);
    if (!current) throw new Error(`unknown effect intent ${intentIdValue}`);
    this.blockEffect(current.id, safetyVerdict, reason);
    return this.getEffectByIntent(intentIdValue)!;
  }

  /** Intent-keyed CAS primitive used by the durable safety ledger. */
  claimEffect(
    intentIdValue: string,
    grantIdValue: string,
    owner: string,
    token: string,
    leaseUntil: string,
    startedAt: string,
    now: string,
    limits: EffectClaimSafetyLimits,
  ): EffectClaimResult {
    return this.db.transaction((): EffectClaimResult => {
      const effect = this.db
        .query(
          `SELECT intent_id, type, args_json, args_sha256, scope_json, lineage_id, lineage_json,
                  deadline_at, action_class, cost_usd, dedupe_hash, state, grant_id
             FROM effects WHERE intent_id = ?`,
        )
        .get(intentIdValue) as {
          intent_id: string;
          type: string;
          args_json: string;
          args_sha256: string;
          scope_json: string;
          lineage_id: string;
          lineage_json: string | null;
          deadline_at: string | null;
          action_class: string | null;
          cost_usd: number;
          dedupe_hash: string | null;
          state: EffectState;
          grant_id: string | null;
        } | null;
      if (!effect || effect.state !== "pending" || effect.grant_id !== grantIdValue) {
        return { claimed: false, code: "conflict", blocked: false };
      }

      const block = (code: EffectClaimBlockCode): EffectClaimResult => {
        const updated = this.db.query(
          `UPDATE effects SET state = 'blocked', safety_verdict = ?
             WHERE intent_id = ? AND state = 'pending' AND grant_id = ?`,
        ).run(code, intentIdValue, grantIdValue);
        if (updated.changes > 0) {
          this.audit("daemon", "effect.blocked_before_claim", "effect", intentIdValue, { code });
          return { claimed: false, code, blocked: true };
        }
        return { claimed: false, code: "conflict", blocked: false };
      };

      const panicRow = this.db.query("SELECT value_json FROM kv WHERE key = 'safety.panic'").get() as { value_json: string } | null;
      if (panicRow) {
        try {
          if ((JSON.parse(panicRow.value_json) as { active?: unknown }).active === true) return block("panic");
        } catch {
          // A corrupt safety state is not evidence that execution is safe.
          return block("panic");
        }
      }
      if (effect.deadline_at !== null && effect.deadline_at <= now) return block("deadline_expired");
      if (limits.dangerousArgsSha256 !== null && limits.dangerousArgsSha256 === effect.args_sha256) {
        return block("dangerous_content");
      }

      const grant = this.db
        .query(
          `SELECT status, uses_remaining, expires_at, effect_type, scope_json,
                  lineage_id, constraint_json
             FROM grants WHERE id = ?`,
        )
        .get(grantIdValue) as {
          status: GrantStatus;
          uses_remaining: number | null;
          expires_at: string | null;
          effect_type: string;
          scope_json: string;
          lineage_id: string | null;
          constraint_json: string;
        } | null;
      if (!grant) return block("grant_mismatch");
      if (grant.status !== "active") {
        return block(grant.status === "revoked" ? "grant_revoked" : grant.status === "expired" ? "grant_expired" : "grant_consumed");
      }
      if (grant.expires_at !== null && grant.expires_at <= now) return block("grant_expired");
      if (grant.uses_remaining !== null && grant.uses_remaining <= 0) return block("grant_consumed");

      let grantScope: Record<string, unknown>;
      let effectScope: Record<string, unknown>;
      let constraints: Record<string, unknown>;
      try {
        grantScope = JSON.parse(grant.scope_json) as Record<string, unknown>;
        effectScope = JSON.parse(effect.scope_json) as Record<string, unknown>;
        constraints = JSON.parse(grant.constraint_json) as Record<string, unknown>;
      } catch {
        return block("grant_mismatch");
      }
      const scopeMatches = Object.entries(grantScope).every(([key, value]) => stableJson(effectScope[key]) === stableJson(value));
      const effectLineage = effect.lineage_json ?? effect.lineage_id;
      const argsMatch = Object.hasOwn(constraints, "args") && stableJson(constraints.args) === effect.args_json;
      const actionClassMatch = constraints.action_class === undefined || constraints.action_class === effect.action_class;
      const deadlineCeiling = constraints.max_deadline_ms;
      const deadlineWithinGrant = deadlineCeiling === undefined || (
        typeof deadlineCeiling === "number" &&
        effect.deadline_at !== null &&
        Date.parse(effect.deadline_at) - Date.parse(now) <= deadlineCeiling
      );
      if (
        grant.effect_type !== effect.type ||
        !scopeMatches ||
        (grant.lineage_id !== null && grant.lineage_id !== effectLineage) ||
        !argsMatch ||
        !actionClassMatch ||
        !deadlineWithinGrant
      ) return block("grant_mismatch");

      const attemptCutoff = new Date(Date.parse(now) - limits.attemptWindowMs).toISOString();
      const failureCutoff = new Date(Date.parse(now) - limits.failureWindowMs).toISOString();
      const spendCutoff = new Date(Date.parse(now) - limits.spendWindowMs).toISOString();
      const dedupeCutoff = new Date(Date.parse(now) - limits.dedupeWindowMs).toISOString();
      const claimedStates = "state IN ('running', 'terminal_recorded')";
      if (effect.dedupe_hash !== null) {
        const duplicate = this.db.query(
          `SELECT 1 AS found FROM effects
             WHERE ${claimedStates} AND dedupe_hash = ? AND started_at >= ? LIMIT 1`,
        ).get(effect.dedupe_hash, dedupeCutoff);
        if (duplicate) return block("dedupe");
      }
      if (limits.maxFailuresPerWindow > 0) {
        const failures = this.db.query(
          `SELECT COUNT(*) AS count FROM effects
             WHERE state = 'terminal_recorded' AND terminal_outcome IN ('failed', 'uncertain')
               AND COALESCE(finished_at, started_at) >= ?`,
        ).get(failureCutoff) as { count: number };
        if (failures.count >= limits.maxFailuresPerWindow) return block("circuit_breaker");
      }
      if (limits.maxAttemptsPerWindow > 0) {
        const attempts = this.db.query(
          `SELECT COUNT(*) AS count FROM effects WHERE ${claimedStates} AND started_at >= ?`,
        ).get(attemptCutoff) as { count: number };
        if (attempts.count >= limits.maxAttemptsPerWindow) return block("rate_limit");
      }
      if (limits.maxSpendUsd > 0) {
        const spend = this.db.query(
          `SELECT COALESCE(SUM(cost_usd), 0) AS cost FROM effects
             WHERE ${claimedStates} AND started_at >= ?`,
        ).get(spendCutoff) as { cost: number };
        if (spend.cost + effect.cost_usd > limits.maxSpendUsd) return block("budget");
      }

      const result = this.db
        .query(
          `UPDATE effects SET state = 'running', claim_owner = ?, claim_token = ?, lease_until = ?,
               started_at = COALESCE(started_at, ?)
             WHERE intent_id = ? AND state = 'pending' AND grant_id = ?`,
        )
        .run(owner, token, leaseUntil, startedAt, intentIdValue, grantIdValue);
      if (result.changes === 0) return { claimed: false, code: "conflict", blocked: false };
      if (grant.uses_remaining !== null) {
        const consumed = this.db
          .query(
            `UPDATE grants SET status = CASE WHEN uses_remaining = 1 THEN 'consumed' ELSE 'active' END,
               uses_remaining = uses_remaining - 1,
               consumed_at = CASE WHEN uses_remaining = 1 THEN ? ELSE consumed_at END
             WHERE id = ? AND status = 'active' AND (expires_at IS NULL OR expires_at >= ?)
               AND uses_remaining > 0`,
          )
          .run(now, grantIdValue, now);
        if (consumed.changes === 0) throw new Error(`grant ${grantIdValue} lost its execution reservation`);
        this.audit("daemon", "grant.consumed", "grant", grantIdValue, { intent_id: intentIdValue });
      } else {
        this.audit("daemon", "grant.used", "grant", grantIdValue, { intent_id: intentIdValue });
      }
      this.audit("daemon", "effect.claimed", "effect", intentIdValue, { owner, leaseUntil, grant_id: grantIdValue });
      return { claimed: true };
    })();
  }

  renewEffect(intentIdValue: string, owner: string, token: string, leaseUntil: string, now: string): boolean {
    const result = this.db
      .query(
        `UPDATE effects SET lease_until = ? WHERE intent_id = ? AND state = 'running'
            AND claim_owner = ? AND claim_token = ? AND (lease_until IS NULL OR lease_until > ?)`,
      )
      .run(leaseUntil, intentIdValue, owner, token, now);
    return result.changes > 0;
  }

  recordTerminalEffect(
    intentIdValue: string,
    owner: string,
    token: string,
    outcome: EffectTerminalOutcome,
    finishedAt: string,
    resultValue: unknown,
    now: string,
  ): boolean {
    const current = this.getEffectByIntent(intentIdValue);
    if (!current) return false;
    if (current.state === "terminal_recorded") {
      if (current.terminal_outcome !== outcome) {
        throw new IdempotencyConflictError("effect", intentIdValue, current.terminal_outcome, outcome, "terminal", current.id);
      }
      return true;
    }
    const changed = this.db
      .query(
        `UPDATE effects SET state = 'terminal_recorded', terminal_outcome = ?, finished_at = ?,
             result_json = ?, claim_owner = NULL, claim_token = NULL, lease_until = NULL
           WHERE intent_id = ? AND state = 'running' AND claim_owner = ? AND claim_token = ?
             AND (lease_until IS NULL OR lease_until > ?)`,
      )
      .run(outcome, finishedAt, resultValue == null ? null : asJson(resultValue), intentIdValue, owner, token, now);
    if (changed.changes > 0) {
      this.audit("daemon", "effect.terminal_recorded", "effect", intentIdValue, { outcome });
      return true;
    }
    return false;
  }

  /** Authoritative, idempotent effect upsert used by the safety ledger. */
  upsertEffect(input: EffectInput): { inserted: boolean; id: string } {
    const existing = this.getEffectByIntent(input.intentId);
    if (!existing) return this.createEffect(input);
    const now = this.clock.now().toISOString();
    const identityHash = payloadSha256({
      type: input.type,
      args: input.args,
      lineage_id: input.lineageId,
      lineage: input.lineage ?? null,
      scope: input.scope ?? {},
      decision_id: input.decisionId ?? null,
    });
    return this.db.transaction(() => {
      const reservation = this.reserveIdentity("effect", input.intentId, identityHash, "effect", existing.id, now);
      const scopeJson = asJson(input.scope ?? {});
      const lineageJson = input.lineage === undefined || input.lineage === null ? null : asJson(input.lineage);
      this.db
        .query(
          `UPDATE effects SET type = ?, args_json = ?, args_sha256 = ?, lineage_id = ?,
             scope_json = ?, lineage_json = ?, deadline_at = ?, action_class = ?, cost_usd = ?,
             provider_policy_verdict = ?, safety_verdict = ?, grant_id = ?, dedupe_hash = ?,
             state = ?, terminal_outcome = ?, claim_owner = ?, claim_token = ?, lease_until = ?,
             started_at = ?, finished_at = ?, result_json = ? WHERE intent_id = ?`,
        )
        .run(
          input.type,
          asJson(input.args),
          payloadSha256(input.args),
          lineageJson ?? input.lineageId,
          scopeJson,
          lineageJson,
          input.deadlineAt ?? null,
          input.actionClass ?? null,
          input.costUsd ?? 0,
          input.providerPolicyVerdict ?? null,
          input.safetyVerdict ?? existing.safety_verdict,
          input.grantId ?? null,
          input.dedupeHash ?? null,
          input.state ?? existing.state,
          input.terminalOutcome ?? existing.terminal_outcome,
          input.claimOwner ?? existing.claim_owner,
          input.claimToken ?? existing.claim_token,
          input.leaseUntil ?? existing.lease_until,
          input.startedAt ?? existing.started_at,
          input.finishedAt ?? existing.finished_at,
          input.result === undefined ? existing.result_json : asJson(input.result),
          input.intentId,
        );
      return { inserted: false, id: existing.id };
    })();
  }

  blockEffect(id: string, safetyVerdict: string, reason?: unknown): void {
    const updated = this.db
      .query("UPDATE effects SET state = 'blocked', safety_verdict = ?, result_json = ? WHERE id = ? AND state IN ('proposed','pending')")
      .run(safetyVerdict, reason === undefined ? null : asJson(reason), id);
    if (updated.changes === 0) {
      const row = this.getEffect(id);
      if (!row) throw new Error(`unknown effect ${id}`);
      if (row.state === "blocked") return;
      throw new Error(`effect ${id} cannot be blocked from ${row.state}`);
    }
    this.audit("daemon", "effect.blocked", "effect", id, { safety_verdict: safetyVerdict });
  }

  claimPendingEffects(limit: number, owner = "effect", leaseSeconds = 120): EffectRow[] {
    const now = this.clock.now();
    const nowIso = now.toISOString();
    const until = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    return this.db.transaction(() => {
      const rows = this.db
        .query(
          `SELECT * FROM effects WHERE state = 'pending' ORDER BY created_at ASC LIMIT ?`,
        )
        .all(limit) as EffectRow[];
      for (const row of rows) {
        const token = claimToken("effect");
        const updated = this.db
          .query(
            `UPDATE effects SET state = 'running', claim_owner = ?, claim_token = ?, lease_until = ?,
               started_at = COALESCE(started_at, ?)
             WHERE id = ? AND state = 'pending'`,
          )
          .run(owner, token, until, nowIso, row.id);
        if (updated.changes === 0) continue;
        row.state = "running";
        row.claim_owner = owner;
        row.claim_token = token;
        row.lease_until = until;
        row.started_at ??= nowIso;
      }
      return rows.filter((row) => row.claim_owner === owner && row.lease_until === until);
    })();
  }

  renewEffectLease(id: string, claim: Claim, leaseSeconds: number): boolean {
    const until = new Date(this.clock.now().getTime() + leaseSeconds * 1000).toISOString();
    const updated = this.db
      .query("UPDATE effects SET lease_until = ? WHERE id = ? AND state = 'running' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?")
      .run(until, id, claim.owner, claim.token, this.clock.now().toISOString());
    return updated.changes > 0;
  }

  /** Record terminal truth before any delivery, observation, or rendering side effect. */
  recordEffectTerminal(
    id: string,
    claim: Claim,
    outcome: EffectTerminalOutcome,
    result?: unknown,
  ): void {
    const now = this.clock.now().toISOString();
    const current = this.getEffect(id);
    if (!current) throw new Error(`unknown effect ${id}`);
    if (current.state === "terminal_recorded") {
      if (current.terminal_outcome !== outcome) {
        throw new IdempotencyConflictError("effect", current.intent_id, current.terminal_outcome, outcome, "effect", id);
      }
      return;
    }
    const updated = this.db
      .query(
        `UPDATE effects SET state = 'terminal_recorded', terminal_outcome = ?, finished_at = ?,
           result_json = ?, claim_owner = NULL, claim_token = NULL, lease_until = NULL
         WHERE id = ? AND state = 'running' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
      )
      .run(outcome, now, result === undefined ? null : asJson(result), id, claim.owner, claim.token, now);
    if (updated.changes === 0) throw new StaleClaimError("effect", id);
    this.audit("daemon", "effect.terminal_recorded", "effect", id, { outcome });
  }

  recordEffectTerminalByToken(id: string, owner: string, token: string, outcome: EffectTerminalOutcome, result?: unknown): void {
    this.recordEffectTerminal(id, { owner, token }, outcome, result);
  }

  recordInteraction(input: InteractionInput): InteractionResult {
    const now = this.clock.now().toISOString();
    const id = input.id ?? interactionId();
    const factId = humanFactId();
    const hash = payloadSha256(input);
    return this.db.transaction(() => {
      const reservation = this.reserveIdentity(
        `interaction:${input.sourceId}`,
        input.idempotencyKey,
        hash,
        "interaction",
        id,
        now,
      );
      if (!reservation.inserted) {
        const existing = this.db
          .query("SELECT id FROM human_facts WHERE interaction_id = ?")
          .get(reservation.objectId) as { id: string } | null;
        if (!existing) throw new Error(`interaction reservation has no fact for ${reservation.objectId}`);
        return { inserted: false, interactionId: reservation.objectId, factId: existing.id };
      }
      this.db
        .query(
          `INSERT INTO interactions
           (id, source_id, idempotency_key, payload_sha256, kind, target_type, target_id,
            actor_id, state, expires_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?)`,
        )
        .run(
          id,
          input.sourceId,
          input.idempotencyKey,
          hash,
          input.kind,
          input.targetType,
          input.targetId,
          input.actorId,
          input.expiresAt ?? null,
          now,
          now,
        );
      this.db
        .query(
          `INSERT INTO human_facts
           (id, interaction_id, kind, actor_id, target_type, target_id, body_json, lineage_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .run(
          factId,
          id,
          input.kind,
          input.actorId,
          input.targetType,
          input.targetId,
          asJson(input.body),
          input.lineageId ?? null,
          now,
        );
      this.audit(input.actorId, "human_fact.recorded", "human_fact", factId, {
        interaction_id: id,
        kind: input.kind,
        target_id: input.targetId,
      });
      const createdGrant = input.grant ? this.insertGrant(input.grant, now) : null;
      return { inserted: true, interactionId: id, factId, ...(createdGrant ? { grantId: createdGrant.id } : {}) };
    })();
  }

  recordHumanFact(input: InteractionInput): InteractionResult {
    return this.recordInteraction(input);
  }

  getGrant(id: string): GrantRow | null {
    return (this.db.query("SELECT * FROM grants WHERE id = ?").get(id) as GrantRow | null) ?? null;
  }

  listGrants(): GrantRow[] {
    return this.db.query("SELECT * FROM grants ORDER BY created_at ASC, id ASC").all() as GrantRow[];
  }

  transitionInteraction(id: string, state: InteractionState, terminalOutcome?: string): boolean {
    const now = this.clock.now().toISOString();
    const updated = this.db
      .query("UPDATE interactions SET state = ?, terminal_outcome = COALESCE(?, terminal_outcome), updated_at = ? WHERE id = ?")
      .run(state, terminalOutcome ?? null, now, id);
    if (updated.changes > 0) this.audit("daemon", "interaction.state", "interaction", id, { state });
    return updated.changes > 0;
  }

  consumeInteraction(id: string): boolean {
    return this.transitionInteraction(id, "consumed", "consumed");
  }

  createGrant(input: GrantInput): { inserted: boolean; id: string } {
    const now = this.clock.now().toISOString();
    return this.db.transaction(() => this.insertGrant(input, now))();
  }

  /**
   * Idempotent grant upsert for the safety ledger. Authority-defining fields are
   * fingerprinted; lifecycle fields (status, use count, timestamps) may advance
   * without reusing the intent as a different grant.
   */
  upsertGrant(input: GrantInput): { inserted: boolean; id: string } {
    const existing = this.db
      .query("SELECT id FROM grants WHERE intent_id = ?")
      .get(input.intentId) as { id: string } | null;
    if (!existing) return this.createGrant(input);
    const now = this.clock.now().toISOString();
    return this.db.transaction(() => {
      this.reserveIdentity("grant", input.intentId, grantIdentityHash(input), "grant", existing.id, now);
      this.db
        .query(
          `UPDATE grants SET scope_json = ?, effect_type = ?, constraint_json = ?,
             status = ?, uses_remaining = ?, expires_at = ?, consumed_at = ?, revoked_at = ?,
             provenance_json = ? WHERE intent_id = ?`,
        )
        .run(
          asJson(input.scope),
          input.effectType,
          asJson(input.constraints ?? {}),
          input.status ?? "active",
          input.usesRemaining ?? null,
          input.expiresAt ?? null,
          input.consumedAt ?? null,
          input.revokedAt ?? null,
          asJson(input.provenance ?? {}),
          input.intentId,
        );
      this.audit("daemon", "grant.updated", "grant", existing.id, { status: input.status ?? "active" });
      return { inserted: false, id: existing.id };
    })();
  }

  transitionGrant(
    id: string,
    status: GrantStatus,
    options: { usesRemaining?: number | null; consumedAt?: string | null; revokedAt?: string | null } = {},
  ): boolean {
    const result = this.db
      .query(
        `UPDATE grants SET status = ?, uses_remaining = COALESCE(?, uses_remaining),
           consumed_at = COALESCE(?, consumed_at), revoked_at = COALESCE(?, revoked_at) WHERE id = ?`,
      )
      .run(status, options.usesRemaining ?? null, options.consumedAt ?? null, options.revokedAt ?? null, id);
    if (result.changes > 0) this.audit("daemon", "grant.state", "grant", id, { status });
    return result.changes > 0;
  }

  private insertGrant(input: GrantInput, now: string): { inserted: boolean; id: string } {
    const id = input.id ?? grantId();
    const hash = grantIdentityHash(input);
    const reservation = this.reserveIdentity("grant", input.intentId, hash, "grant", id, now);
    if (!reservation.inserted) return { inserted: false, id: reservation.objectId };
    this.db
      .query(
        `INSERT INTO grants
         (id, intent_id, lineage_id, scope_json, effect_type, constraint_json, status,
          uses_remaining, created_by, created_at, expires_at, consumed_at, revoked_at, provenance_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        input.intentId,
        input.lineageId ?? null,
        asJson(input.scope),
        input.effectType,
        asJson(input.constraints ?? {}),
        input.status ?? "active",
        input.usesRemaining ?? null,
        input.createdBy,
        now,
        input.expiresAt ?? null,
        input.consumedAt ?? null,
        input.revokedAt ?? null,
        asJson(input.provenance ?? {}),
      );
    this.audit(input.createdBy, "grant.created", "grant", id, { effect_type: input.effectType });
    return { inserted: true, id };
  }

  consumeGrant(id: string): boolean {
    const now = this.clock.now().toISOString();
    return this.db.transaction(() => {
      const row = this.db
        .query("SELECT status, uses_remaining, expires_at FROM grants WHERE id = ?")
        .get(id) as { status: GrantStatus; uses_remaining: number | null; expires_at: string | null } | null;
      if (!row || row.status !== "active" || isExpired(row.expires_at, now)) return false;
      const result = this.db
        .query(
          `UPDATE grants SET status = CASE WHEN uses_remaining = 1 THEN 'consumed' ELSE 'active' END,
             uses_remaining = CASE WHEN uses_remaining IS NULL THEN NULL ELSE uses_remaining - 1 END,
             consumed_at = CASE WHEN uses_remaining = 1 THEN ? ELSE consumed_at END
           WHERE id = ? AND status = 'active' AND (expires_at IS NULL OR expires_at >= ?)
             AND (uses_remaining IS NULL OR uses_remaining > 0)`,
        )
        .run(now, id, now);
      if (result.changes > 0) this.audit("daemon", "grant.consumed", "grant", id, {});
      return result.changes > 0;
    })();
  }

  revokeGrant(id: string, actor = "human"): boolean {
    const now = this.clock.now().toISOString();
    const result = this.db
      .query("UPDATE grants SET status = 'revoked', revoked_at = ? WHERE id = ? AND status = 'active'")
      .run(now, id);
    if (result.changes > 0) this.audit(actor, "grant.revoked", "grant", id, {});
    return result.changes > 0;
  }

  expireGrants(): number {
    const now = this.clock.now().toISOString();
    const result = this.db
      .query("UPDATE grants SET status = 'expired' WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?")
      .run(now);
    if (result.changes > 0) this.audit("daemon", "grant.expired", "grant", "batch", { count: result.changes });
    return result.changes;
  }

  claimPendingOutbox(limit: number, owner = "outbox", leaseSeconds = 120): OutboxRow[] {
    const now = this.clock.now();
    const nowIso = now.toISOString();
    const until = new Date(now.getTime() + leaseSeconds * 1000).toISOString();
    return this.db.transaction(() => {
      const rows = this.db
        .query(
          `SELECT * FROM outbox
           WHERE state = 'pending' AND next_attempt_at <= ?
           ORDER BY id ASC LIMIT ?`,
        )
        .all(nowIso, limit) as OutboxRow[];
      const claimed: OutboxRow[] = [];
      for (const row of rows) {
        const token = claimToken("outbox");
        const updated = this.db
          .query(
            `UPDATE outbox SET state = 'sending', claim_owner = ?, claim_token = ?, lease_until = ?
             WHERE id = ? AND state = 'pending'`,
          )
          .run(owner, token, until, row.id);
        if (updated.changes === 0) continue;
        row.state = "sending";
        row.claim_owner = owner;
        row.claim_token = token;
        row.lease_until = until;
        claimed.push(row);
      }
      return claimed;
    })();
  }

  renewOutboxLease(id: number, claim: Claim, leaseSeconds: number): boolean {
    const until = new Date(this.clock.now().getTime() + leaseSeconds * 1000).toISOString();
    const result = this.db
      .query("UPDATE outbox SET lease_until = ? WHERE id = ? AND state = 'sending' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?")
      .run(until, id, claim.owner, claim.token, this.clock.now().toISOString());
    return result.changes > 0;
  }

  /** A transport rejection is known-not-delivered and may be retried safely. */
  rescheduleOutbox(id: number, claim: Claim, nextAttemptAt: string, error: unknown): boolean {
    const now = this.clock.now().toISOString();
    const result = this.db.query(
      `UPDATE outbox SET state = 'pending', attempts = attempts + 1, next_attempt_at = ?,
         result_json = ?, claim_owner = NULL, claim_token = NULL, lease_until = NULL
       WHERE id = ? AND state = 'sending' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
    ).run(nextAttemptAt, asJson({ error: String(error) }), id, claim.owner, claim.token, now);
    if (result.changes > 0) this.audit("daemon", "outbox.retry", "outbox", String(id), { next_attempt_at: nextAttemptAt, error: String(error) });
    return result.changes > 0;
  }

  /** Hold a claimed intent for digest delivery without bypassing its fence. */
  deferOutbox(id: number, claim: Claim): boolean {
    const now = this.clock.now().toISOString();
    const result = this.db.query(
      `UPDATE outbox SET state = 'deferred', claim_owner = NULL, claim_token = NULL, lease_until = NULL
       WHERE id = ? AND state = 'sending' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
    ).run(id, claim.owner, claim.token, now);
    return result.changes > 0;
  }

  /**
   * Persist a channel receipt only after the transport attempt. A crash in the
   * remote-send window is represented by `uncertain`, never converted to an
   * ordinary retry that could silently duplicate a message.
   */
  recordOutboxReceipt(
    id: number,
    claim: Claim,
    outcome: Extract<OutboxState, "delivered" | "uncertain" | "failed" | "expired" | "superseded" | "abandoned" | "suppressed" | "no_target">,
    options: { sentMessageId?: string | null; error?: unknown; transportResult?: unknown } = {},
  ): void {
    const receiptAt = this.clock.now().toISOString();
    const current = this.db.query("SELECT state FROM outbox WHERE id = ?").get(id) as { state: string } | null;
    if (!current) throw new Error(`unknown outbox row ${id}`);
    if (["delivered", "uncertain", "failed", "expired", "superseded", "abandoned", "suppressed", "no_target"].includes(current.state)) {
      if (current.state !== outcome) {
        throw new IdempotencyConflictError("outbox", String(id), current.state, outcome, "outbox", String(id));
      }
      return;
    }
    const result = this.db
      .query(
        `UPDATE outbox SET state = ?, attempts = attempts + 1, sent_message_id = COALESCE(?, sent_message_id),
           result_json = ?,
           recovery_state = CASE WHEN ? = 'uncertain' THEN 'remote_delivery_unknown' ELSE recovery_state END,
           claim_owner = NULL, claim_token = NULL, lease_until = NULL
           WHERE id = ? AND state = 'sending' AND claim_owner = ? AND claim_token = ? AND lease_until >= ?`,
      )
      .run(
        outcome,
        options.sentMessageId ?? null,
        asJson({
          receipt_at: receiptAt,
          ...(options.error === undefined ? {} : { error: options.error }),
          ...(options.transportResult === undefined ? {} : { transport_result: options.transportResult }),
        }),
        outcome,
        id,
        claim.owner,
        claim.token,
        this.clock.now().toISOString(),
      );
    if (result.changes === 0) throw new StaleClaimError("outbox", String(id));
    this.audit("daemon", `outbox.${outcome}`, "outbox", String(id), {
      sent_message_id: options.sentMessageId ?? null,
      error: options.error,
      receipt_at: receiptAt,
    });
  }

  /**
   * Startup replay surface. Rows remain the authority; callers can feed these
   * lists to their workers without relying on process handles or mirror files.
   */
  replayPendingWork(): {
    events: EventRow[];
    providerInvocations: ProviderInvocationRow[];
    effects: EffectRow[];
    outbox: OutboxRow[];
  } {
    const now = this.clock.now().toISOString();
    return {
      events: this.db
        .query(
          `SELECT * FROM events WHERE triage_state = 'pending'
             OR (triage_state = 'coalescing' AND COALESCE(route_lease_until, triage_lease_until) < ?)
           ORDER BY received_at ASC`,
        )
        .all(now) as EventRow[],
      providerInvocations: this.db
        .query(
          `SELECT * FROM provider_invocations WHERE state = 'pending'
             OR (state = 'running' AND lease_until < ?) ORDER BY created_at ASC`,
        )
        .all(now) as ProviderInvocationRow[],
      effects: this.db
        .query(
          `SELECT * FROM effects WHERE state = 'pending' ORDER BY created_at ASC`,
        )
        .all() as EffectRow[],
      // `sending` rows are intentionally excluded after expiry: their remote
      // outcome is unknown and must be reconciled rather than blindly retried.
      outbox: this.db
        .query(
          `SELECT * FROM outbox WHERE state = 'pending' AND next_attempt_at <= ? ORDER BY id ASC`,
        )
        .all(now) as OutboxRow[],
    };
  }

  /** Mark non-terminal work recoverable at startup without inventing success. */
  recoverExpiredClaims(): { events: number; providerInvocations: number; effects: number; outbox: number } {
    const now = this.clock.now().toISOString();
    return this.db.transaction(() => {
      const events = (this.db
        .query(
          `UPDATE events SET triage_state = 'pending', route_state = 'pending', triage_claim_owner = NULL,
             triage_claim_token = NULL, triage_lease_until = NULL, route_claim_owner = NULL,
             route_claim_token = NULL, route_lease_until = NULL
           WHERE triage_state = 'coalescing' AND COALESCE(route_lease_until, triage_lease_until) < ?`,
        )
        .run(now).changes ?? 0);
      const providerInvocations = (this.db
        .query(
          `UPDATE provider_invocations SET state = 'pending', recovery_state = 'lease_expired',
             claim_owner = NULL, claim_token = NULL, lease_until = NULL
           WHERE state = 'running' AND lease_until < ?`,
        )
        .run(now).changes ?? 0);
      const effects = (this.db
        .query(
          `UPDATE effects SET state = 'terminal_recorded', terminal_outcome = 'uncertain',
             finished_at = ?, recovery_state = 'execution_outcome_unknown',
             result_json = COALESCE(result_json, ?), claim_owner = NULL,
             claim_token = NULL, lease_until = NULL
           WHERE state = 'running' AND lease_until < ?`,
        )
        .run(now, asJson({ recovery: "lease_expired", outcome: "uncertain" }), now).changes ?? 0);
      // An expired outbox lease follows the uncertain-delivery path. It is not
      // eligible for automatic retry until a channel reconciliation resolves it.
      const outbox = (this.db
        .query(
          `UPDATE outbox SET state = 'uncertain', recovery_state = 'remote_delivery_unknown',
             claim_owner = NULL, claim_token = NULL, lease_until = NULL
           WHERE state = 'sending' AND lease_until < ?`,
        )
        .run(now).changes ?? 0);
      for (const [objectType, count] of Object.entries({ events, providerInvocations, effects, outbox })) {
        if (count > 0) this.audit("daemon", "work.recovered", objectType, "batch", { count });
      }
      return { events, providerInvocations, effects, outbox };
    })();
  }

  recordSpend(provider: string, model: string, tokensIn: number, tokensOut: number, costUsd: number): void {
    const day = this.clock.now().toISOString().slice(0, 10);
    this.db
      .query(
        `INSERT INTO spend (day, provider, model, calls, tokens_in, tokens_out, cost_usd)
         VALUES (?, ?, ?, 1, ?, ?, ?)
         ON CONFLICT(day, provider, model) DO UPDATE SET
           calls = calls + 1, tokens_in = tokens_in + excluded.tokens_in,
           tokens_out = tokens_out + excluded.tokens_out, cost_usd = cost_usd + excluded.cost_usd`,
      )
      .run(day, provider, model, tokensIn, tokensOut, costUsd);
  }

  spendToday(): { cost_usd: number; calls: number } {
    const day = this.clock.now().toISOString().slice(0, 10);
    const row = this.db
      .query("SELECT COALESCE(SUM(cost_usd),0) cost_usd, COALESCE(SUM(calls),0) calls FROM spend WHERE day = ?")
      .get(day) as { cost_usd: number; calls: number };
    return row;
  }

  kvGet<T>(key: string): T | null {
    const row = this.db.query("SELECT value_json FROM kv WHERE key = ?").get(key) as
      | { value_json: string }
      | null;
    return row ? (JSON.parse(row.value_json) as T) : null;
  }

  kvSet(key: string, value: unknown): void {
    this.db
      .query(
        `INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)
         ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at`,
      )
      .run(key, JSON.stringify(value), this.clock.now().toISOString());
  }

  kvDelete(key: string): void {
    this.db.query("DELETE FROM kv WHERE key = ?").run(key);
  }

  claimDaemonOwner(owner: string, leaseSeconds = 30): { owner: string; token: string; leaseUntil: string } | null {
    const now = this.clock.now();
    const at = now.toISOString();
    const leaseUntil = new Date(now.getTime() + leaseSeconds * 1_000).toISOString();
    return this.db.transaction(() => {
      const existing = this.db.query("SELECT owner, lease_until FROM daemon_leases WHERE name = 'card'").get() as { owner: string; lease_until: string } | null;
      if (existing && existing.lease_until > at) return null;
      const token = claimToken("daemon");
      this.db.query(
        `INSERT INTO daemon_leases (name, owner, token, lease_until, acquired_at, renewed_at)
         VALUES ('card', ?, ?, ?, ?, ?)
         ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, token = excluded.token,
           lease_until = excluded.lease_until, acquired_at = excluded.acquired_at,
           renewed_at = excluded.renewed_at
         WHERE daemon_leases.lease_until <= ?`,
      ).run(owner, token, leaseUntil, at, at, at);
      const row = this.db.query("SELECT owner, token, lease_until FROM daemon_leases WHERE name = 'card'").get() as { owner: string; token: string; lease_until: string } | null;
      if (!row || row.owner !== owner || row.token !== token) return null;
      this.audit("daemon", existing ? "daemon.owner_reclaimed" : "daemon.owner_claimed", "daemon", "card", { owner, lease_until: leaseUntil });
      return { owner, token, leaseUntil };
    })();
  }

  renewDaemonOwner(claim: { owner: string; token: string }, leaseSeconds = 30): boolean {
    const now = this.clock.now();
    const at = now.toISOString();
    const leaseUntil = new Date(now.getTime() + leaseSeconds * 1_000).toISOString();
    const result = this.db.query(
      `UPDATE daemon_leases SET lease_until = ?, renewed_at = ?
       WHERE name = 'card' AND owner = ? AND token = ? AND lease_until >= ?`,
    ).run(leaseUntil, at, claim.owner, claim.token, at);
    return result.changes > 0;
  }

  releaseDaemonOwner(claim: { owner: string; token: string }): boolean {
    return this.db.query("DELETE FROM daemon_leases WHERE name = 'card' AND owner = ? AND token = ?")
      .run(claim.owner, claim.token).changes > 0;
  }

  getPanicState(): PanicState {
    return this.kvGet<PanicState>("safety.panic") ?? { active: false, reason: null, changedAt: null };
  }

  setPanic(reason = "manual panic"): PanicState {
    const state: PanicState = { active: true, reason, changedAt: this.clock.now().toISOString() };
    this.kvSet("safety.panic", state);
    this.audit("human", "safety.panic", "safety", "global", { reason });
    return state;
  }

  clearPanic(): PanicState {
    const state: PanicState = { active: false, reason: null, changedAt: this.clock.now().toISOString() };
    this.kvSet("safety.panic", state);
    this.audit("human", "safety.panic_cleared", "safety", "global", {});
    return state;
  }
}

export function openStore(path: string, clock: Clock = systemClock): Store {
  return new Store(openDb(path), clock);
}
