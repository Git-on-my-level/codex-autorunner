/**
 * Typed read helpers over the frozen schema. Reads only — every write in this
 * module goes through an explicit statement in handlers.ts and audits.
 */
import type { Store } from "../../store/db.ts";
import type { ResponseChannel } from "../../contract/events.ts";

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

export interface DecisionRow {
  id: string;
  incident_id: string;
  decided_by: string;
  disposition: string;
  action_class: string | null;
  action_args_json: string | null;
  rationale: string;
  model: string | null;
  cost_usd: number;
  created_at: string;
}

export function getEscalation(store: Store, id: string): EscalationRow | null {
  return (store.db.query("SELECT * FROM escalations WHERE id = ?").get(id) as EscalationRow | null) ?? null;
}

export function getEscalationByMessage(store: Store, messageId: string): EscalationRow | null {
  return (
    (store.db
      .query("SELECT * FROM escalations WHERE telegram_message_id = ? ORDER BY created_at DESC LIMIT 1")
      .get(messageId) as EscalationRow | null) ?? null
  );
}

export function getIncident(store: Store, id: string): IncidentRow | null {
  return (store.db.query("SELECT * FROM incidents WHERE id = ?").get(id) as IncidentRow | null) ?? null;
}

export function latestDecision(store: Store, incidentId: string): DecisionRow | null {
  return (
    (store.db
      .query("SELECT * FROM decisions WHERE incident_id = ? ORDER BY created_at DESC, id DESC LIMIT 1")
      .get(incidentId) as DecisionRow | null) ?? null
  );
}

/**
 * The reply route for an incident: the response_channel of the event that
 * opened it, falling back to the most recent event on the incident that wanted
 * a response. Null means "file fallback only" — WS-E handles that.
 */
export function incidentResponseChannel(store: Store, incident: IncidentRow): ResponseChannel | null {
  const row = store.db
    .query(
      `SELECT response_channel_json FROM events
       WHERE id = ?
          OR (incident_id = ? AND requires_response = 1)
       ORDER BY (id = ?) DESC, received_at DESC LIMIT 1`,
    )
    .get(incident.opened_by_event, incident.id, incident.opened_by_event) as
    | { response_channel_json: string | null }
    | null;
  if (!row?.response_channel_json) return null;
  try {
    return JSON.parse(row.response_channel_json) as ResponseChannel;
  } catch {
    return null;
  }
}

/** The event type that opened an incident (drives approve/deny affordance + rule scope). */
export function openingEventType(store: Store, incident: IncidentRow): string | null {
  const row = store.db
    .query("SELECT type FROM events WHERE id = ?")
    .get(incident.opened_by_event) as { type: string } | null;
  return row?.type ?? null;
}

/**
 * Does the suggested action point at approve or deny? Null when CAR had no
 * opinion — the tap is then recorded, but as neither confirmation nor override.
 */
export function suggestedApproval(suggestedActionJson: string | null): boolean | null {
  if (!suggestedActionJson) return null;
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(suggestedActionJson) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (typeof parsed.approval === "boolean") return parsed.approval;
  const text = `${String(parsed.class ?? "")} ${String(parsed.action_class ?? "")} ${String(parsed.label ?? "")}`.toLowerCase();
  if (text.includes("deny") || text.includes("reject")) return false;
  if (text.includes("approve") || text.includes("allow")) return true;
  return null;
}

export function suggestedLabel(suggestedActionJson: string | null): string | undefined {
  if (!suggestedActionJson) return undefined;
  try {
    const parsed = JSON.parse(suggestedActionJson) as Record<string, unknown>;
    if (typeof parsed.label === "string" && parsed.label) return parsed.label;
    const approval = suggestedApproval(suggestedActionJson);
    if (approval === true) return "APPROVE";
    if (approval === false) return "DENY";
    if (typeof parsed.class === "string") return parsed.class;
  } catch {
    /* ignore */
  }
  return undefined;
}
