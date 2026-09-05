/**
 * Read-only SQL helpers for the web UI. Every function here is a SELECT against
 * deps.store.db — no writes. Mutations live in writes.ts and go through Store /
 * MemoryWriter so every one of them audits.
 */
import type { Database } from "bun:sqlite";
import { AGENT_RUN_SUCCESS_STATES, AGENT_RUN_TERMINAL_STATES } from "../../contract/lifecycle.ts";

export interface EventRow {
  id: string;
  ts: string;
  received_at: string;
  type: string;
  severity: string;
  requires_response: number;
  title: string;
  body: string;
  car_session_id: string | null;
  incident_id: string | null;
  triage_state: string;
  source_vendor: string;
  source_host: string;
  source_adapter: string;
  payload_json: string;
  response_channel_json: string | null;
  native_agent: string;
  session_title: string | null;
  session_repo: string | null;
  session_vendor: string | null;
  session_cwd: string | null;
  session_repo_verified: number | null;
}

export interface InboxFilters {
  vendor?: string;
  severity?: string;
  state?: string;
  repo?: string;
  q?: string;
  before?: string;
  limit?: number;
}

export function listEvents(db: Database, filters: InboxFilters): { rows: EventRow[]; hasMore: boolean } {
  const limit = filters.limit ?? 50;
  // Progress and heartbeat remain durable evidence, but the human Inbox is an
  // attention surface. Run lifecycle density belongs in /runs.
  const clauses: string[] = ["e.type NOT IN ('progress', 'heartbeat')"];
  const params: (string | number)[] = [];
  if (filters.vendor) {
    clauses.push(`(CASE WHEN e.source_vendor = 'agentctl'
      THEN COALESCE(json_extract(e.payload_json, '$.agentctl.adapter'), 'agentctl')
      ELSE e.source_vendor END) = ?`);
    params.push(filters.vendor);
  }
  if (filters.severity) {
    clauses.push("e.severity = ?");
    params.push(filters.severity);
  }
  if (filters.state) {
    clauses.push("e.triage_state = ?");
    params.push(filters.state);
  }
  if (filters.repo) {
    clauses.push("s.repo = ?");
    params.push(filters.repo);
  }
  if (filters.q) {
    clauses.push("(e.title LIKE ? OR e.body LIKE ?)");
    params.push(`%${filters.q}%`, `%${filters.q}%`);
  }
  if (filters.before) {
    clauses.push("e.received_at < ?");
    params.push(filters.before);
  }
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  const sql = `
    SELECT e.id, e.ts, e.received_at, e.type, e.severity, e.requires_response, e.title, e.body,
           e.car_session_id, e.incident_id, e.triage_state, e.source_vendor, e.source_host, e.source_adapter,
           e.payload_json, e.response_channel_json,
           CASE WHEN e.source_vendor = 'agentctl'
             THEN COALESCE(json_extract(e.payload_json, '$.agentctl.adapter'), 'agentctl')
             ELSE e.source_vendor END AS native_agent,
           s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor,
           s.cwd AS session_cwd, s.repo_verified AS session_repo_verified
    FROM events e
    LEFT JOIN sessions s ON e.car_session_id = s.car_session_id
    ${where}
    ORDER BY e.received_at DESC
    LIMIT ?`;
  const rows = db.query(sql).all(...params, limit + 1) as EventRow[];
  const hasMore = rows.length > limit;
  return { rows: rows.slice(0, limit), hasMore };
}

export interface IncidentRow {
  id: string;
  car_session_id: string | null;
  opened_by_event: string;
  state: string;
  snooze_until: string | null;
  summary: string;
  dedupe_class: string | null;
  llm_runs: number;
  opened_at: string;
  closed_at: string | null;
  session_title: string | null;
  session_repo: string | null;
  session_vendor: string | null;
  requires_operator_response: number;
  response_agent: string | null;
  response_channel_kind: string | null;
  continuation_vendor: string | null;
}

export function listIncidents(db: Database, state?: string): IncidentRow[] {
  const where = state ? "WHERE i.state = ?" : "WHERE i.state IN ('open','escalated')";
  const sql = `
    SELECT i.id, i.car_session_id, i.opened_by_event, i.state, i.snooze_until, i.summary,
           i.dedupe_class, i.llm_runs, i.opened_at, i.closed_at,
           s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor,
           (SELECT CASE WHEN e2.source_vendor = 'agentctl'
             THEN COALESCE(json_extract(e2.payload_json, '$.agentctl.adapter'), 'agentctl')
             ELSE e2.source_vendor END
            FROM events e2 WHERE e2.incident_id = i.id
            ORDER BY e2.received_at DESC LIMIT 1) AS response_agent,
           (SELECT json_extract(e2.response_channel_json, '$.kind')
            FROM events e2 WHERE e2.incident_id = i.id AND e2.response_channel_json IS NOT NULL
            ORDER BY e2.received_at DESC LIMIT 1) AS response_channel_kind,
           (SELECT sr.vendor FROM session_refs sr
            WHERE sr.car_session_id = i.car_session_id AND sr.vendor IN ('codex','claude-code','claude')
            ORDER BY CASE sr.vendor WHEN 'codex' THEN 0 ELSE 1 END LIMIT 1) AS continuation_vendor,
           EXISTS(
             SELECT 1 FROM escalations e
             WHERE e.incident_id = i.id AND e.answer_json IS NULL AND e.state = 'pending'
           ) AS requires_operator_response
    FROM incidents i
    LEFT JOIN sessions s ON i.car_session_id = s.car_session_id
    ${where}
    ORDER BY i.opened_at DESC
    LIMIT 200`;
  return (state ? db.query(sql).all(state) : db.query(sql).all()) as IncidentRow[];
}

export function getIncident(db: Database, id: string): IncidentRow | null {
  const row = db
    .query(
      `SELECT i.id, i.car_session_id, i.opened_by_event, i.state, i.snooze_until, i.summary,
              i.dedupe_class, i.llm_runs, i.opened_at, i.closed_at,
              s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor,
              (SELECT CASE WHEN e2.source_vendor = 'agentctl'
                THEN COALESCE(json_extract(e2.payload_json, '$.agentctl.adapter'), 'agentctl')
                ELSE e2.source_vendor END
               FROM events e2 WHERE e2.incident_id = i.id
               ORDER BY e2.received_at DESC LIMIT 1) AS response_agent,
              (SELECT json_extract(e2.response_channel_json, '$.kind')
               FROM events e2 WHERE e2.incident_id = i.id AND e2.response_channel_json IS NOT NULL
               ORDER BY e2.received_at DESC LIMIT 1) AS response_channel_kind,
              (SELECT sr.vendor FROM session_refs sr
               WHERE sr.car_session_id = i.car_session_id AND sr.vendor IN ('codex','claude-code','claude')
               ORDER BY CASE sr.vendor WHEN 'codex' THEN 0 ELSE 1 END LIMIT 1) AS continuation_vendor,
              EXISTS(
                SELECT 1 FROM escalations e
                WHERE e.incident_id = i.id AND e.answer_json IS NULL AND e.state = 'pending'
              ) AS requires_operator_response
       FROM incidents i
       LEFT JOIN sessions s ON i.car_session_id = s.car_session_id
       WHERE i.id = ?`,
    )
    .get(id) as IncidentRow | null;
  return row ?? null;
}

export interface DecisionRow {
  id: string;
  incident_id: string;
  decided_by: string;
  disposition: string;
  action_class: string | null;
  action_args_json: string | null;
  rationale: string;
  model: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at: string;
}

export interface ActionRow {
  id: string;
  decision_id: string;
  class: string;
  args_json: string;
  policy_verdict: string;
  dedupe_hash: string;
  state: string;
  started_at: string | null;
  finished_at: string | null;
  result_json: string | null;
}

export interface EscalationRow {
  id: string;
  incident_id: string;
  severity: string;
  question: string;
  suggested_action_json: string | null;
  state: string;
  telegram_message_id: string | null;
  sent_at: string | null;
  answered_by: string | null;
  answer_json: string | null;
  answered_at: string | null;
  created_at: string;
}

export interface OutcomeRow {
  id: string;
  decision_id: string;
  escalation_id: string | null;
  verdict: string;
  david_action_json: string | null;
  note: string | null;
  created_at: string;
}

export interface AuditRow {
  id: number;
  ts: string;
  actor: string;
  verb: string;
  object_type: string;
  object_id: string;
  detail_json: string;
}

export interface IncidentChain {
  incident: IncidentRow;
  events: EventRow[];
  decisions: DecisionRow[];
  actions: ActionRow[];
  escalations: EscalationRow[];
  outcomes: OutcomeRow[];
  audit: AuditRow[];
}

function inClause(n: number): string {
  return `(${Array.from({ length: n }, () => "?").join(",")})`;
}

/** Assembles the full "why did CAR do that" chain for one incident. */
export function getIncidentChain(db: Database, incidentId: string): IncidentChain | null {
  const incident = getIncident(db, incidentId);
  if (!incident) return null;

  const events = db
    .query(
      `SELECT e.id, e.ts, e.received_at, e.type, e.severity, e.requires_response, e.title, e.body,
              e.car_session_id, e.incident_id, e.triage_state, e.source_vendor, e.source_host, e.source_adapter,
              e.payload_json, e.response_channel_json,
              CASE WHEN e.source_vendor = 'agentctl'
                THEN COALESCE(json_extract(e.payload_json, '$.agentctl.adapter'), 'agentctl')
                ELSE e.source_vendor END AS native_agent,
              s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor,
              s.cwd AS session_cwd, s.repo_verified AS session_repo_verified
       FROM events e
       LEFT JOIN sessions s ON e.car_session_id = s.car_session_id
       WHERE e.incident_id = ?
       ORDER BY e.received_at ASC`,
    )
    .all(incidentId) as EventRow[];

  const decisions = db
    .query(`SELECT * FROM decisions WHERE incident_id = ? ORDER BY created_at ASC`)
    .all(incidentId) as DecisionRow[];
  const decisionIds = decisions.map((d) => d.id);

  const actions = decisionIds.length
    ? (db
        .query(`SELECT * FROM actions WHERE decision_id IN ${inClause(decisionIds.length)} ORDER BY started_at ASC`)
        .all(...decisionIds) as ActionRow[])
    : [];

  const escalations = db
    .query(`SELECT * FROM escalations WHERE incident_id = ? ORDER BY created_at ASC`)
    .all(incidentId) as EscalationRow[];
  const escalationIds = escalations.map((e) => e.id);

  let outcomes: OutcomeRow[] = [];
  if (decisionIds.length && escalationIds.length) {
    outcomes = db
      .query(
        `SELECT * FROM outcomes WHERE decision_id IN ${inClause(decisionIds.length)}
           OR escalation_id IN ${inClause(escalationIds.length)} ORDER BY created_at ASC`,
      )
      .all(...decisionIds, ...escalationIds) as OutcomeRow[];
  } else if (decisionIds.length) {
    outcomes = db
      .query(`SELECT * FROM outcomes WHERE decision_id IN ${inClause(decisionIds.length)} ORDER BY created_at ASC`)
      .all(...decisionIds) as OutcomeRow[];
  } else if (escalationIds.length) {
    outcomes = db
      .query(`SELECT * FROM outcomes WHERE escalation_id IN ${inClause(escalationIds.length)} ORDER BY created_at ASC`)
      .all(...escalationIds) as OutcomeRow[];
  }

  const eventIds = events.map((e) => e.id);
  const actionIds = actions.map((a) => a.id);
  const auditObjectIds = [incidentId, ...eventIds, ...decisionIds, ...escalationIds, ...actionIds];
  const audit = auditObjectIds.length
    ? (db
        .query(`SELECT * FROM audit WHERE object_id IN ${inClause(auditObjectIds.length)} ORDER BY ts ASC`)
        .all(...auditObjectIds) as AuditRow[])
    : [];

  return { incident, events, decisions, actions, escalations, outcomes, audit };
}

export interface AgentRunViewRow {
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
  observation_state: string;
  last_meaningful_update: string | null;
  first_observed_at: string;
  last_observed_at: string;
}

export interface AgentObserverHealth {
  state: "ok" | "degraded" | "disabled" | "not_started" | "stale";
  observed_at?: string;
  error?: string;
  run_count?: number;
  coverage_degraded?: boolean;
  history_truncated?: boolean;
}

export type AgentRunStateFilter = "all" | "active" | "attention" | "finished";

export interface AgentRunFilters {
  state: AgentRunStateFilter;
  agent?: string;
  page?: number;
  limit?: number;
  observerReliable: boolean;
}

export interface AgentRunSummary {
  active: number;
  attention: number;
  finished: number;
  total: number;
}

export interface AgentRunList {
  rows: AgentRunViewRow[];
  total: number;
  page: number;
  hasNext: boolean;
  agents: string[];
}

const RUN_SUCCESS_SQL = AGENT_RUN_SUCCESS_STATES.map((state) => `'${state}'`).join(",");
const RUN_TERMINAL_SQL = AGENT_RUN_TERMINAL_STATES.map((state) => `'${state}'`).join(",");
const runTerminalSql = (alias = "r") => `(${alias}.terminal_at IS NOT NULL OR lower(${alias}.state) IN (${RUN_TERMINAL_SQL}))`;
const runSuccessSql = (alias = "r") => `lower(${alias}.state) IN (${RUN_SUCCESS_SQL})`;
const runNativeAttentionSql = (alias = "r") =>
  `(lower(${alias}.state) = 'attention' OR lower(COALESCE(${alias}.liveness,'')) = 'blocked')`;
const runActiveSql = (observerReliable: boolean, alias = "r") =>
  `NOT ${runTerminalSql(alias)} AND NOT ${runNativeAttentionSql(alias)} AND ${observerReliable ? `${alias}.observation_state NOT IN ('stale','unknown') AND COALESCE(lower(${alias}.liveness), '') != 'unreachable'` : "0"}`;
const runAttentionSql = (observerReliable: boolean, alias = "r") =>
  `((${runTerminalSql(alias)}) AND NOT ${runSuccessSql(alias)}) OR (NOT ${runTerminalSql(alias)} AND ${observerReliable ? `(${runNativeAttentionSql(alias)} OR ${alias}.observation_state IN ('stale','unknown') OR lower(COALESCE(${alias}.liveness,'')) = 'unreachable')` : "1"})`;

export function listAgentRuns(db: Database, filters: AgentRunFilters): AgentRunList {
  const limit = Math.min(100, Math.max(10, filters.limit ?? 50));
  const page = Math.max(0, filters.page ?? 0);
  const clauses: string[] = [];
  const params: (string | number)[] = [];
  if (filters.agent) {
    clauses.push("r.agent = ?");
    params.push(filters.agent);
  }
  if (filters.state === "active") clauses.push(runActiveSql(filters.observerReliable));
  if (filters.state === "attention") clauses.push(runAttentionSql(filters.observerReliable));
  if (filters.state === "finished") clauses.push(runSuccessSql());
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  const total = (db.query(`SELECT COUNT(*) n FROM agent_runs r ${where}`).get(...params) as { n: number }).n;
  const rows = db.query(
    `SELECT execution_id, transport, agent, authority, mode, state, liveness,
            labels_json, title, repo, cwd, profile, model, runtime,
            continuation_supported, started_at, updated_at, terminal_at,
            duration_seconds, observation_state, last_meaningful_update,
            first_observed_at, last_observed_at
     FROM agent_runs r ${where}
     ORDER BY CASE
       WHEN ${runActiveSql(filters.observerReliable)} THEN 0
       WHEN ${runAttentionSql(filters.observerReliable)} THEN 1
       ELSE 2 END,
       COALESCE(terminal_at, updated_at) DESC, execution_id DESC
     LIMIT ? OFFSET ?`,
  ).all(...params, limit, page * limit) as AgentRunViewRow[];
  const agents = (db.query("SELECT DISTINCT agent FROM agent_runs ORDER BY agent").all() as { agent: string }[])
    .map((row) => row.agent);
  return { rows, total, page, hasNext: (page + 1) * limit < total, agents };
}

export function getAgentRunSummary(db: Database, observerReliable: boolean): AgentRunSummary {
  const row = db.query(
    `SELECT
       SUM(CASE WHEN ${runActiveSql(observerReliable)} THEN 1 ELSE 0 END) active,
       SUM(CASE WHEN ${runAttentionSql(observerReliable)} THEN 1 ELSE 0 END) attention,
       SUM(CASE WHEN ${runSuccessSql()} THEN 1 ELSE 0 END) finished,
       COUNT(*) total
     FROM agent_runs r`,
  ).get() as { active: number | null; attention: number | null; finished: number | null; total: number };
  return {
    active: row.active ?? 0,
    attention: row.attention ?? 0,
    finished: row.finished ?? 0,
    total: row.total,
  };
}

export function getAgentObserverHealth(db: Database): AgentObserverHealth {
  const row = db.query("SELECT value_json FROM kv WHERE key = 'agentctl.observer.health'").get() as { value_json: string } | null;
  if (!row) return { state: "not_started" };
  try {
    return JSON.parse(row.value_json) as AgentObserverHealth;
  } catch {
    return { state: "degraded", error: "Observer health record is unreadable" };
  }
}

export interface MemoryRow {
  id: string;
  tier: string;
  scope_json: string;
  kind: string;
  content_json: string;
  confidence: number;
  evidence_confirm: number;
  evidence_override: number;
  autonomy: string;
  status: string;
  authored_by: string;
  created_at: string;
  updated_at: string;
}

export function listRules(db: Database): MemoryRow[] {
  return db
    .query(`SELECT * FROM memories WHERE tier = 'rule' AND status = 'active' ORDER BY confidence DESC`)
    .all() as MemoryRow[];
}

export function listNotes(db: Database, limit = 50): MemoryRow[] {
  return db
    .query(`SELECT * FROM memories WHERE tier = 'note' AND status != 'archived' ORDER BY created_at DESC LIMIT ?`)
    .all(limit) as MemoryRow[];
}

export function listPendingMemories(db: Database): MemoryRow[] {
  return db.query(`SELECT * FROM memories WHERE status = 'pending' ORDER BY created_at ASC`).all() as MemoryRow[];
}

export function getMemory(db: Database, id: string): MemoryRow | null {
  return (db.query(`SELECT * FROM memories WHERE id = ?`).get(id) as MemoryRow | null) ?? null;
}

export interface DigestRow {
  day: string;
  rendered_md: string;
  sent_at: string | null;
  outbox_id: number | null;
  outbox_state: string | null;
  outbox_target_json: string | null;
  outbox_result_json: string | null;
  sent_message_id: string | null;
  outbox_created_at: string | null;
}

export function listDigests(db: Database, limit = 30): DigestRow[] {
  return db.query(
    `SELECT d.day, d.rendered_md, d.sent_at, d.outbox_id,
            o.state AS outbox_state, o.target_json AS outbox_target_json,
            o.result_json AS outbox_result_json, o.sent_message_id, o.created_at AS outbox_created_at
     FROM digests d
     LEFT JOIN outbox o ON o.id = d.outbox_id
     ORDER BY d.day DESC LIMIT ?`,
  ).all(limit) as DigestRow[];
}

export function getDigest(db: Database, day: string): DigestRow | null {
  return (db.query(
    `SELECT d.day, d.rendered_md, d.sent_at, d.outbox_id,
            o.state AS outbox_state, o.target_json AS outbox_target_json,
            o.result_json AS outbox_result_json, o.sent_message_id, o.created_at AS outbox_created_at
     FROM digests d LEFT JOIN outbox o ON o.id = d.outbox_id WHERE d.day = ?`,
  ).get(day) as DigestRow | null) ?? null;
}
