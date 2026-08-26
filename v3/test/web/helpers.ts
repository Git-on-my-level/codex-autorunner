/**
 * Test-only helpers for the web UI suite. Builds a full DaemonDeps against the
 * in-memory fakes (test/fakes.ts) plus the real memory writer (already wired
 * with real DB writes + audit in src/memory/index.ts), and mounts the web
 * sub-app under its real "/ui" path the same way daemon.ts does.
 */
import { Hono } from "hono";
import type { Database } from "bun:sqlite";
import {
  memoryStore,
  testConfig,
  FakeActionBus,
  FakeChannel,
  AllowAllPolicy,
  EmptyMemoryReader,
} from "../fakes.ts";
import { createMemory } from "../../src/memory/index.ts";
import { createWebUi } from "../../src/surfaces/web/index.ts";
import type { Store, Clock } from "../../src/store/db.ts";
import type { CarConfig } from "../../src/config/config.ts";
import type { DaemonDeps, TriagePort } from "../../src/ports.ts";
import { incidentId as newIncidentId, decisionId as newDecisionId, escalationId as newEscalationId, outcomeId as newOutcomeId, memoryId as newMemoryId, actionId as newActionId } from "../../src/contract/ids.ts";

export function buildDeps(opts: { store?: Store; config?: CarConfig } = {}): DaemonDeps {
  const store = opts.store ?? memoryStore();
  const config = opts.config ?? testConfig();
  const { writer } = createMemory(store, config);
  const noopTriage: TriagePort = { tick: async () => 0 };
  return {
    store,
    config,
    triage: noopTriage,
    actions: new FakeActionBus(),
    channel: new FakeChannel(),
    memoryReader: new EmptyMemoryReader(),
    memoryWriter: writer,
    policy: new AllowAllPolicy(),
  };
}

/** Mounts the sub-app the same way daemon.ts's ingest server does: app.route(path, app). */
export function mountApp(deps: DaemonDeps): Hono {
  const { path, app } = createWebUi(deps);
  const root = new Hono();
  root.route(path, app);
  return root;
}

/* --------------------------------------------------------- direct seeding */
/* Incidents/decisions/escalations/outcomes have no repository methods yet
 * (owned by WS-B/others) — seed them with direct INSERTs, per the task's own
 * "Seed via memoryStore + direct INSERTs" instruction. */

export function seedIncident(
  db: Database,
  clock: Clock,
  overrides: Partial<{
    id: string;
    car_session_id: string | null;
    opened_by_event: string;
    state: string;
    summary: string;
    dedupe_class: string | null;
    llm_runs: number;
  }> = {},
): string {
  const id = overrides.id ?? newIncidentId();
  db.query(
    `INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, dedupe_class, llm_runs, opened_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    overrides.car_session_id ?? null,
    overrides.opened_by_event ?? "evt_seed",
    overrides.state ?? "open",
    overrides.summary ?? "",
    overrides.dedupe_class ?? null,
    overrides.llm_runs ?? 0,
    clock.now().toISOString(),
  );
  return id;
}

export function seedDecision(
  db: Database,
  clock: Clock,
  incidentId: string,
  overrides: Partial<{
    id: string;
    decided_by: string;
    disposition: string;
    action_class: string | null;
    action_args_json: string | null;
    rationale: string;
    model: string | null;
    tokens_in: number;
    tokens_out: number;
    cost_usd: number;
  }> = {},
): string {
  const id = overrides.id ?? newDecisionId();
  db.query(
    `INSERT INTO decisions (id, incident_id, decided_by, disposition, action_class, action_args_json,
       rationale, model, tokens_in, tokens_out, cost_usd, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    incidentId,
    overrides.decided_by ?? "llm",
    overrides.disposition ?? "escalate",
    overrides.action_class ?? null,
    overrides.action_args_json ?? null,
    overrides.rationale ?? "",
    overrides.model ?? null,
    overrides.tokens_in ?? 0,
    overrides.tokens_out ?? 0,
    overrides.cost_usd ?? 0,
    clock.now().toISOString(),
  );
  return id;
}

export function seedAction(
  db: Database,
  decisionId: string,
  overrides: Partial<{
    id: string;
    class: string;
    args_json: string;
    policy_verdict: string;
    dedupe_hash: string;
    state: string;
    result_json: string | null;
  }> = {},
): string {
  const id = overrides.id ?? newActionId();
  db.query(
    `INSERT INTO actions (id, decision_id, class, args_json, policy_verdict, dedupe_hash, state, result_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    decisionId,
    overrides.class ?? "probe.example",
    overrides.args_json ?? "{}",
    overrides.policy_verdict ?? "auto",
    overrides.dedupe_hash ?? "hash1",
    overrides.state ?? "ok",
    overrides.result_json ?? null,
  );
  return id;
}

export function seedEscalation(
  db: Database,
  clock: Clock,
  incidentId: string,
  overrides: Partial<{
    id: string;
    severity: string;
    question: string;
    suggested_action_json: string | null;
    state: string;
    answered_by: string | null;
    answer_json: string | null;
    answered_at: string | null;
  }> = {},
): string {
  const id = overrides.id ?? newEscalationId();
  const now = clock.now().toISOString();
  db.query(
    `INSERT INTO escalations (id, incident_id, severity, question, suggested_action_json, state,
       answered_by, answer_json, answered_at, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    incidentId,
    overrides.severity ?? "attention",
    overrides.question ?? "What now?",
    overrides.suggested_action_json ?? null,
    overrides.state ?? "pending",
    overrides.answered_by ?? null,
    overrides.answer_json ?? null,
    overrides.answered_at ?? null,
    now,
  );
  return id;
}

export function seedOutcome(
  db: Database,
  clock: Clock,
  overrides: Partial<{
    id: string;
    decision_id: string;
    escalation_id: string | null;
    verdict: string;
    david_action_json: string | null;
    note: string | null;
  }> = {},
): string {
  const id = overrides.id ?? newOutcomeId();
  db.query(
    `INSERT INTO outcomes (id, decision_id, escalation_id, verdict, david_action_json, note, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    overrides.decision_id ?? "",
    overrides.escalation_id ?? null,
    overrides.verdict ?? "confirmed",
    overrides.david_action_json ?? null,
    overrides.note ?? null,
    clock.now().toISOString(),
  );
  return id;
}

export function seedMemory(
  db: Database,
  clock: Clock,
  overrides: Partial<{
    id: string;
    tier: string;
    scope: Record<string, unknown>;
    kind: string;
    content: Record<string, unknown>;
    confidence: number;
    evidence_confirm: number;
    evidence_override: number;
    autonomy: string;
    status: string;
    authored_by: string;
  }> = {},
): string {
  const id = overrides.id ?? newMemoryId();
  const now = clock.now().toISOString();
  db.query(
    `INSERT INTO memories (id, tier, scope_json, kind, content_json, confidence, evidence_confirm,
       evidence_override, autonomy, status, authored_by, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    id,
    overrides.tier ?? "rule",
    JSON.stringify(overrides.scope ?? {}),
    overrides.kind ?? "preference",
    JSON.stringify(overrides.content ?? { match: { type: "attention.permission" }, disposition: "auto_resolve" }),
    overrides.confidence ?? 0.5,
    overrides.evidence_confirm ?? 0,
    overrides.evidence_override ?? 0,
    overrides.autonomy ?? "none",
    overrides.status ?? "active",
    overrides.authored_by ?? "triage",
    now,
    now,
  );
  return id;
}
