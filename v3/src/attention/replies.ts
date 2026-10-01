/** Human input + response intent commit together. Sending is a separate, restartable worker. */
import { createHash } from "node:crypto";
import type { Store } from "../store/db.ts";
import type { ActionBus, Loop, ReplyPayload, ChannelPort } from "../ports.ts";
import type { ResponseChannel } from "../contract/events.ts";
import { stableJson } from "../contract/ids.ts";
import { AttentionError } from "./errors.ts";

export interface ReplyRow {
  id: string; escalation_id: string | null; request_id: string | null;
  incident_id: string | null; event_id: string | null; car_session_id: string | null;
  response_channel_json: string | null; payload_json: string; actor: string;
  state: "pending" | "delivering" | "delivered" | "staged" | "uncertain" | "failed" | "acknowledged" | "resolved" | "cancelled" | "expired";
  revision: number; attempts: number; last_error: string | null; created_at: string; updated_at: string;
  acknowledged_at: string | null; resolved_at: string | null;
}
export const stableId = (prefix: string, value: unknown): string =>
  `${prefix}_${createHash("sha256").update(stableJson(value)).digest("hex").slice(0, 36)}`;
export function getReply(store: Store, id: string): ReplyRow | null {
  return store.db.query("SELECT * FROM human_replies WHERE id = ?").get(id) as ReplyRow | null;
}

/** Reply intent is a strict one-of at every shared boundary, not just in UI schemas. */
function validReplyPayload(payload: unknown): payload is ReplyPayload {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const keys = Object.keys(payload);
  if (keys.length !== 1 || (keys[0] !== "text" && keys[0] !== "approval")) return false;
  const value = payload as Record<string, unknown>;
  return keys[0] === "text"
    ? typeof value.text === "string" && value.text.trim().length > 0 && value.text.length <= 8_000
    : typeof value.approval === "boolean";
}

function requireReplyPayload(payload: unknown): asserts payload is ReplyPayload {
  if (!validReplyPayload(payload)) {
    throw new AttentionError("invalid_answer", "Provide one nonempty text answer OR one approval decision", 400);
  }
}

/** Only authenticated human surfaces call this function. It creates no standing grant. */
export function recordAnswer(store: Store, input: {
  escalationId: string; actor: string; payload: ReplyPayload;
  expectedMessageId?: string; expectedRevision?: number;
}): ReplyRow {
  if (!input.actor) throw new AttentionError("invalid_answer", "A human actor is required", 400);
  requireReplyPayload(input.payload);
  return store.db.transaction(() => {
    const esc = store.db.query(`SELECT s.*, i.car_session_id, e.id AS event_id, e.response_channel_json,
        e.expires_at, e.obligation_state, r.id AS request_id, r.revision AS request_revision, r.state AS request_state
      FROM escalations s JOIN incidents i ON i.id = s.incident_id
      JOIN events e ON e.id = COALESCE(s.origin_event_id, i.opened_by_event)
      LEFT JOIN attention_requests r ON r.escalation_id = s.id WHERE s.id = ?`).get(input.escalationId) as {
      id: string; state: string; incident_id: string; car_session_id: string | null;
      event_id: string; response_channel_json: string | null; expires_at: string | null;
      obligation_state: string; telegram_message_id: string | null;
      request_id: string | null; request_revision: number | null; request_state: string | null;
    } | null;
    if (!esc) throw new AttentionError("not_found", "Decision no longer exists", 404);
    if (input.expectedMessageId !== undefined && esc.telegram_message_id !== input.expectedMessageId) {
      throw new AttentionError("stale_card", "Use the current decision card");
    }
    if (input.expectedRevision !== undefined && esc.request_revision !== input.expectedRevision) {
      throw new AttentionError("revision_conflict", "The decision changed. Review the current version before answering.");
    }
    const id = stableId("reply", { escalation: esc.id });
    const existing = getReply(store, id);
    if (existing) {
      if (stableJson(JSON.parse(existing.payload_json)) !== stableJson(input.payload)) {
        throw new AttentionError("already_answered", "Another answer has already been recorded");
      }
      return existing;
    }
    const now = store.clock.now().toISOString();
    if (esc.state !== "pending" || ["resolved", "cancelled", "expired"].includes(esc.obligation_state) ||
        (esc.request_id && esc.request_state !== "needs_you") || (esc.expires_at && esc.expires_at <= now)) {
      throw new AttentionError("stale_decision", "The request is no longer awaiting an answer");
    }
    const payload = JSON.stringify(input.payload);
    store.db.query(`INSERT INTO human_replies (id, escalation_id, request_id, incident_id, event_id,
        car_session_id, response_channel_json, payload_json, actor, state, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(id, esc.id, esc.request_id, esc.incident_id,
      esc.event_id, esc.car_session_id, esc.response_channel_json, payload, input.actor,
      esc.request_id ? "staged" : "pending", now, now);
    store.db.query("UPDATE escalations SET state='answered', answer_json=?, answered_by=?, answered_at=? WHERE id=?")
      .run(payload, input.actor, now, esc.id);
    store.db.query("UPDATE events SET obligation_state='answered' WHERE id=?").run(esc.event_id);
    store.db.query("UPDATE incidents SET state='open', closed_at=NULL, snooze_until=NULL WHERE id=?").run(esc.incident_id);
    if (esc.request_id) store.db.query("UPDATE attention_requests SET state='answered', updated_at=? WHERE id=?").run(now, esc.request_id);
    store.recordHumanFact({ sourceId: "core:human-answers", idempotencyKey: id, kind: "reply",
      targetType: "escalation", targetId: esc.id, actorId: input.actor, body: { ...input.payload, reply_id: id } });
    store.audit(input.actor, "human.answer_recorded", "human_reply", id, { escalation_id: esc.id, request_id: esc.request_id });
    return getReply(store, id)!;
  })();
}

/** General human instructions also use the same outbox, even without a decision card. */
export function recordSessionReply(store: Store, input: {
  idempotencyKey: string; actor: string; carSessionId: string;
  channel: ResponseChannel | null; payload: ReplyPayload;
}): ReplyRow {
  if (!input.actor) throw new AttentionError("invalid_answer", "A human actor is required", 400);
  requireReplyPayload(input.payload);
  const id = stableId("reply", { source: "human-session", key: input.idempotencyKey });
  const now = store.clock.now().toISOString();
  return store.db.transaction(() => {
    const existing = getReply(store, id);
    if (existing) {
      if (existing.payload_json !== JSON.stringify(input.payload) || existing.car_session_id !== input.carSessionId)
        throw new AttentionError("idempotency_conflict", "Message id already belongs to different content");
      return existing;
    }
    store.db.query(`INSERT INTO human_replies (id, car_session_id, response_channel_json, payload_json, actor, state, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)`).run(id, input.carSessionId, input.channel ? JSON.stringify(input.channel) : null,
      JSON.stringify(input.payload), input.actor, now, now);
    store.audit(input.actor, "human.instruction_recorded", "human_reply", id, { car_session_id: input.carSessionId });
    return getReply(store, id)!;
  })();
}

export function recoverInterruptedReplies(store: Store): number {
  return store.db.transaction(() => {
    const rows = store.db.query("SELECT id FROM human_replies WHERE state='delivering'").all() as { id: string }[];
    for (const row of rows) {
      store.db.query("UPDATE human_replies SET revision=revision+1, state='uncertain', last_error=?, updated_at=? WHERE id=?")
        .run("Restart during send: remote acceptance is unknown; do not automatically resend.", store.clock.now().toISOString(), row.id);
      store.audit("core", "reply.delivery_uncertain", "human_reply", row.id, { reason: "restart_during_send" });
    }
    return rows.length;
  })();
}

export async function deliverReply(store: Store, actions: ActionBus, id: string): Promise<ReplyRow | null> {
  const now = store.clock.now().toISOString();
  const claimed = store.db.query("UPDATE human_replies SET revision=revision+1, state='delivering', attempts=attempts+1, updated_at=? WHERE id=? AND state='pending'").run(now, id);
  if (!claimed.changes) return getReply(store, id);
  const row = getReply(store, id)!;
  const event = row.event_id ? store.db.query("SELECT expires_at, obligation_state, requires_response FROM events WHERE id=?").get(row.event_id) as
    { expires_at: string | null; obligation_state: string; requires_response: number } | null : null;
  let state: ReplyRow["state"] = "uncertain";
  let error: string | null = null;
  if (event?.expires_at && event.expires_at <= now || event && ["expired", "cancelled", "resolved"].includes(event.obligation_state)) {
    state = event?.obligation_state === "cancelled" ? "cancelled" : "expired";
    error = "Request no longer accepts a response; answer retained in history.";
  } else if (!row.car_session_id) {
    // A human can acknowledge a sessionless FYI, but an unanswered native ask
    // with no return route must remain visibly failed, not silently resolved.
    state = event?.requires_response ? "failed" : "resolved";
    error = state === "failed" ? "No return route. Use the source-owned attention API for remote requests." : null;
  } else {
    try {
      const result = await actions.deliver(row.car_session_id,
        row.response_channel_json ? JSON.parse(row.response_channel_json) as ResponseChannel : null,
        JSON.parse(row.payload_json) as ReplyPayload);
      state = result === "delivered" ? "delivered" : result === "failed" ? "failed" : "staged";
      if (state === "failed") error = "No delivery adapter accepted the answer.";
    } catch (err) { error = `Delivery threw; remote outcome unknown: ${String(err).slice(0, 1_000)}`; }
  }
  store.db.transaction(() => {
    const changed = store.db.query("UPDATE human_replies SET revision=revision+1, state=?, last_error=?, updated_at=? WHERE id=? AND state='delivering'")
      .run(state, error, store.clock.now().toISOString(), id);
    if (!changed.changes) return; // Source clearance/cancellation won the race.
    if (row.event_id) store.db.query("UPDATE events SET obligation_state=? WHERE id=? AND obligation_state NOT IN ('resolved','cancelled','expired')")
      .run(state === "resolved" ? "resolved" : state, row.event_id);
    if (state === "resolved" && row.incident_id) store.db.query("UPDATE incidents SET state='resolved', closed_at=? WHERE id=?")
      .run(store.clock.now().toISOString(), row.incident_id);
    store.audit("core", "reply.delivery_observed", "human_reply", id, { state, error });
  })();
  return getReply(store, id);
}

export function createReplyWorker(store: Store, actions: ActionBus, channel?: ChannelPort): Loop & { tick(): Promise<void> } {
  let timer: ReturnType<typeof setInterval> | undefined;
  let running: Promise<void> | null = null;
  const tick = async () => {
    if (running) return running;
    running = (async () => {
      const rows = store.db.query("SELECT id FROM human_replies WHERE state='pending' ORDER BY created_at LIMIT 10").all() as { id: string }[];
      for (const row of rows) await deliverReply(store, actions, row.id);
      if (channel) {
        const failed = store.db.query("SELECT r.id, r.state, r.attempts FROM human_replies r WHERE r.state IN ('failed','uncertain') AND NOT EXISTS (SELECT 1 FROM kv WHERE key='reply.failure_notified.' || r.id || '.' || r.attempts) ORDER BY r.updated_at DESC LIMIT 100").all() as { id: string; state: string; attempts: number }[];
        for (const reply of failed) store.db.transaction(() => {
          const key = `reply.failure_notified.${reply.id}.${reply.attempts}`;
          if (store.kvGet(key)) return;
          channel.sendNotify(`CAR recorded your answer, but delivery is ${reply.state}. Check the source before resending. The Watching view contains the delivery record (${reply.id}).`);
          store.kvSet(key, true);
        })();
      }
    })();
    try { await running; } finally { running = null; }
  };
  return { name: "human-reply-delivery", tick,
    async start() { recoverInterruptedReplies(store); timer = setInterval(() => { void tick().catch((e) => store.audit("core", "reply.worker_failed", "worker", "replies", { error: String(e) })); }, 250); },
    async stop() { if (timer) clearInterval(timer); timer = undefined; await running; },
  };
}

/** Explicit human reconciliation of native delivery uncertainty; never an automatic retry. */
export function reconcileReply(store: Store, input: {
  id: string; expectedRevision: number; actor: string;
  outcome: "source_confirmed" | "not_received_retry" | "cancel"; note: string;
}): ReplyRow {
  if (!input.actor || !input.note.trim() || input.note.length > 2_000) throw new AttentionError("evidence_required", "Explain what was checked at the source", 400);
  return store.db.transaction(() => {
    const row = getReply(store, input.id);
    if (!row) throw new AttentionError("not_found", "Unknown reply", 404);
    if (row.request_id) throw new AttentionError("source_owned", "This request has a source-owned receipt protocol. Let its agent acknowledge or cancel it.");
    if (row.revision !== input.expectedRevision || !["failed", "uncertain", "staged", "delivered"].includes(row.state))
      throw new AttentionError("reply_changed", "Reload the current delivery record before reconciling it");
    const now = store.clock.now().toISOString();
    const event = row.event_id ? store.getEvent(row.event_id) : null;
    // Reconciliation is evidence about delivery, not a way to rewrite a
    // terminal native obligation. Once the source deadline/clearance closes
    // the event, every outcome must preserve that terminal state.
    if (event && ((event.expires_at && event.expires_at <= now) || ["resolved", "cancelled", "expired"].includes(event.obligation_state ?? "")))
      throw new AttentionError("request_closed", "A closed or expired request cannot be reconciled");
    const state = input.outcome === "source_confirmed" ? "resolved" : input.outcome === "not_received_retry" ? "pending" : "cancelled";
    store.db.query("UPDATE human_replies SET revision=revision+1, state=?, last_error=NULL, updated_at=?, resolved_at=? WHERE id=?")
      .run(state, now, state === "resolved" ? now : null, row.id);
    if (row.event_id) store.db.query("UPDATE events SET obligation_state=? WHERE id=?").run(state === "pending" ? "answered" : state, row.event_id);
    if (row.incident_id && state !== "pending") {
      const remaining = store.db.query("SELECT COUNT(*) AS n FROM events WHERE incident_id=? AND requires_response=1 AND obligation_state NOT IN ('resolved','cancelled','expired')")
        .get(row.incident_id) as { n: number };
      if (remaining.n === 0) store.db.query("UPDATE incidents SET state='resolved', closed_at=?, summary=? WHERE id=?")
        .run(now, `Human reconciliation: ${input.outcome}. ${input.note}`, row.incident_id);
    }
    store.recordHumanFact({ sourceId: "core:delivery-reconciliation", idempotencyKey: `${row.id}:${row.revision}:${input.outcome}`, kind: "feedback", targetType: "human_reply", targetId: row.id, actorId: input.actor,
      body: { outcome: input.outcome, note: input.note, previous_state: row.state } });
    store.audit(input.actor, "reply.human_reconciled", "human_reply", row.id, { outcome: input.outcome, note: input.note });
    return getReply(store, row.id)!;
  })();
}
