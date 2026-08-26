/**
 * Triage's write side over the frozen schema: incidents, decisions, escalations,
 * action rows. The scaffold Store exposes no repositories for these tables, so
 * triage writes them directly through `store.db` — every write audits, which is
 * the invariant that actually matters ("EVERYTHING writes to audit").
 */
import type { Store } from "../store/db.ts";
import { actionId, decisionId, escalationId, incidentId } from "../contract/ids.ts";
import type { PolicyVerdict } from "../ports.ts";

export interface IncidentRow {
  id: string;
  car_session_id: string | null;
  opened_by_event: string;
  state: string;
  snooze_until: string | null;
  summary: string;
  telegram_message_id: string | null;
  dedupe_class: string | null;
  llm_runs: number;
  opened_at: string;
  closed_at: string | null;
}

export type Disposition = "auto_resolve" | "keep_informed" | "escalate" | "defer";
export type DecidedBy = "rules" | "llm" | "human";

export interface DecisionInput {
  incidentId: string;
  decidedBy: DecidedBy;
  disposition: Disposition;
  rationale: string;
  actionClass?: string | null;
  actionArgs?: Record<string, unknown> | null;
  model?: string | null;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
  /** Pre-minted id so tool calls made during the run can reference the decision. */
  id?: string;
}

export interface ActionInput {
  id?: string;
  decisionId: string;
  actionClass: string;
  args: Record<string, unknown>;
  policyVerdict: PolicyVerdict | "blocked_dedupe" | "blocked_rate" | "blocked_breaker" | "blocked";
  dedupeHash: string;
  state: "pending" | "running" | "ok" | "failed";
  result?: unknown;
}

/** Lineage lookback: the same dedupe class recurring within a day is one story. */
export const LINEAGE_WINDOW_HOURS = 24;

export class TriageRepo {
  constructor(private readonly store: Store) {}

  private now(): string {
    return this.store.clock.now().toISOString();
  }

  getIncident(id: string): IncidentRow | null {
    return (this.store.db.query("SELECT * FROM incidents WHERE id = ?").get(id) as IncidentRow | null) ?? null;
  }

  /**
   * Most recent incident sharing this session + dedupe class inside the lineage
   * window, whatever its state — recurrence of a *resolved* problem is exactly
   * the case the LLM-run cap exists to catch.
   */
  findLineageIncident(carSessionId: string | null, dedupeClass: string): IncidentRow | null {
    const cutoff = new Date(
      this.store.clock.now().getTime() - LINEAGE_WINDOW_HOURS * 3600_000,
    ).toISOString();
    const sql = carSessionId
      ? `SELECT * FROM incidents WHERE car_session_id = ? AND dedupe_class = ? AND state != 'expired'
           AND opened_at >= ? ORDER BY opened_at DESC LIMIT 1`
      : `SELECT * FROM incidents WHERE car_session_id IS NULL AND dedupe_class = ? AND state != 'expired'
           AND opened_at >= ? ORDER BY opened_at DESC LIMIT 1`;
    const params = carSessionId ? [carSessionId, dedupeClass, cutoff] : [dedupeClass, cutoff];
    return (this.store.db.query(sql).get(...(params as never[])) as IncidentRow | null) ?? null;
  }

  /** Newest incident on a session that CAR's own events should attach to. */
  findOpenIncidentForSession(carSessionId: string): IncidentRow | null {
    return (
      (this.store.db
        .query(
          `SELECT * FROM incidents WHERE car_session_id = ? AND state IN ('open','escalated','snoozed')
            ORDER BY opened_at DESC LIMIT 1`,
        )
        .get(carSessionId) as IncidentRow | null) ?? null
    );
  }

  openIncident(input: {
    carSessionId: string | null;
    openedByEvent: string;
    dedupeClass: string;
    summary?: string;
  }): IncidentRow {
    const id = incidentId();
    const now = this.now();
    this.store.db
      .query(
        `INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, dedupe_class, llm_runs, opened_at)
         VALUES (?, ?, ?, 'open', ?, ?, 0, ?)`,
      )
      .run(id, input.carSessionId, input.openedByEvent, input.summary ?? "", input.dedupeClass, now);
    this.store.audit("triage", "incident.opened", "incident", id, {
      car_session_id: input.carSessionId,
      dedupe_class: input.dedupeClass,
    });
    return this.getIncident(id)!;
  }

  /** Reuse a lineage incident: carries `llm_runs` forward across recurrences. */
  reopenIncident(id: string): IncidentRow {
    this.store.db
      .query("UPDATE incidents SET state = 'open', closed_at = NULL, snooze_until = NULL WHERE id = ?")
      .run(id);
    this.store.audit("triage", "incident.reopened", "incident", id, {});
    return this.getIncident(id)!;
  }

  setIncidentState(
    id: string,
    state: "open" | "resolved" | "escalated" | "snoozed" | "expired",
    opts: { summary?: string; snoozeUntil?: string | null } = {},
  ): void {
    const closed = state === "resolved" || state === "expired" ? this.now() : null;
    this.store.db
      .query(
        `UPDATE incidents SET state = ?, summary = COALESCE(?, summary),
           snooze_until = ?, closed_at = ? WHERE id = ?`,
      )
      .run(state, opts.summary ?? null, opts.snoozeUntil ?? null, closed, id);
    this.store.audit("triage", "incident.state", "incident", id, { state, summary: opts.summary });
  }

  /** Bump and return the new LLM-run count for this incident lineage. */
  incrementLlmRuns(id: string): number {
    this.store.db.query("UPDATE incidents SET llm_runs = llm_runs + 1 WHERE id = ?").run(id);
    const row = this.store.db.query("SELECT llm_runs FROM incidents WHERE id = ?").get(id) as
      | { llm_runs: number }
      | null;
    return row?.llm_runs ?? 0;
  }

  recordDecision(input: DecisionInput): string {
    const id = input.id ?? decisionId();
    this.store.db
      .query(
        `INSERT INTO decisions (id, incident_id, decided_by, disposition, action_class, action_args_json,
           rationale, model, tokens_in, tokens_out, cost_usd, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        input.incidentId,
        input.decidedBy,
        input.disposition,
        input.actionClass ?? null,
        input.actionArgs ? JSON.stringify(input.actionArgs) : null,
        input.rationale,
        input.model ?? null,
        input.tokensIn ?? 0,
        input.tokensOut ?? 0,
        input.costUsd ?? 0,
        this.now(),
      );
    this.store.audit(input.decidedBy, "decision.recorded", "decision", id, {
      incident_id: input.incidentId,
      disposition: input.disposition,
      action_class: input.actionClass ?? null,
      cost_usd: input.costUsd ?? 0,
    });
    return id;
  }

  /**
   * Action rows are the substrate the policy gate counts (rate limits, dedupe,
   * circuit breaker), so triage writes one for every attempted *and blocked*
   * action — a blocked attempt is evidence too.
   */
  recordAction(input: ActionInput): string {
    const id = input.id ?? actionId();
    const now = this.now();
    const terminal = input.state === "ok" || input.state === "failed";
    this.store.db
      .query(
        `INSERT INTO actions (id, decision_id, class, args_json, policy_verdict, dedupe_hash, state,
           started_at, finished_at, result_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        input.decisionId,
        input.actionClass,
        JSON.stringify(input.args),
        input.policyVerdict,
        input.dedupeHash,
        input.state,
        now,
        terminal ? now : null,
        input.result === undefined ? null : JSON.stringify(input.result),
      );
    this.store.audit("triage", "action.recorded", "action", id, {
      class: input.actionClass,
      policy_verdict: input.policyVerdict,
      state: input.state,
      decision_id: input.decisionId,
    });
    return id;
  }

  createEscalation(input: {
    incidentId: string;
    severity: string;
    question: string;
    suggestedAction?: Record<string, unknown> | null;
  }): string {
    const id = escalationId();
    const now = this.now();
    this.store.db
      .query(
        `INSERT INTO escalations (id, incident_id, severity, question, suggested_action_json, state, sent_at, created_at)
         VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)`,
      )
      .run(
        id,
        input.incidentId,
        input.severity,
        input.question,
        input.suggestedAction ? JSON.stringify(input.suggestedAction) : null,
        now,
        now,
      );
    this.store.audit("triage", "escalation.created", "escalation", id, {
      incident_id: input.incidentId,
      severity: input.severity,
    });
    return id;
  }

  /** Move a batch of events out of `coalescing` and onto their incident. */
  attachEvents(eventIds: string[], incidentIdValue: string | null, state: string): void {
    for (const eventIdValue of eventIds) {
      this.store.setEventTriageState(eventIdValue, state, incidentIdValue ?? undefined);
    }
  }
}
