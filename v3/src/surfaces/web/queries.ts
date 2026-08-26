/**
 * Read-only SQL helpers for the web UI. Every function here is a SELECT against
 * deps.store.db — no writes. Mutations live in writes.ts and go through Store /
 * MemoryWriter so every one of them audits.
 */
import type { Database } from "bun:sqlite";

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
  session_title: string | null;
  session_repo: string | null;
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
  const clauses: string[] = [];
  const params: (string | number)[] = [];
  if (filters.vendor) {
    clauses.push("e.source_vendor = ?");
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
           s.title AS session_title, s.repo AS session_repo
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
}

export function listIncidents(db: Database, state?: string): IncidentRow[] {
  const where = state ? "WHERE i.state = ?" : "WHERE i.state IN ('open','escalated')";
  const sql = `
    SELECT i.id, i.car_session_id, i.opened_by_event, i.state, i.snooze_until, i.summary,
           i.dedupe_class, i.llm_runs, i.opened_at, i.closed_at,
           s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor
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
              s.title AS session_title, s.repo AS session_repo, s.vendor AS session_vendor
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
              s.title AS session_title, s.repo AS session_repo
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
}

export function listDigests(db: Database, limit = 30): DigestRow[] {
  return db.query(`SELECT day, rendered_md, sent_at FROM digests ORDER BY day DESC LIMIT ?`).all(limit) as DigestRow[];
}

export function getDigest(db: Database, day: string): DigestRow | null {
  return (db.query(`SELECT day, rendered_md, sent_at FROM digests WHERE day = ?`).get(day) as DigestRow | null) ?? null;
}
