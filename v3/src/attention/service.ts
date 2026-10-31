/** Deterministic request lifecycle, shared by HTTP, CLI, MCP and human surfaces. */
import { createHash } from "node:crypto";
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type { ChannelPort, Loop } from "../ports.ts";
import type { CarEvent } from "../contract/events.ts";
import { stableJson } from "../contract/ids.ts";
import { TriageRepo } from "../triage/repo.ts";
import type { ClientIdentity, DecisionPacket, RequestRow } from "./contract.ts";
import { assessPacket, decisionContext } from "./quality.ts";
import { deadlineElapsed, requestGuidance, terminalRequest } from "./guidance.ts";
export { terminalRequest } from "./guidance.ts";
import { AttentionError } from "./errors.ts";
import { recordAnswer, stableId, type ReplyRow } from "./replies.ts";

const hash = (value: unknown) => createHash("sha256").update(stableJson(value)).digest("hex");
export class AttentionService {
  private readonly repo: TriageRepo;
  constructor(readonly store: Store, readonly config: CarConfig, private readonly channel: ChannelPort) {
    this.repo = new TriageRepo(store);
  }
  private now(): string { return this.store.clock.now().toISOString(); }
  private stored(id: string): RequestRow | null {
    return this.store.db.query("SELECT * FROM attention_requests WHERE id=? AND workspace_id=?")
      .get(id, this.config.attention.workspace_id) as RequestRow | null;
  }
  /** Expiry is checked at the point of use, not only by a background sweep. */
  get(id: string): RequestRow | null {
    const row = this.stored(id);
    if (row && deadlineElapsed(row, this.now())) {
      this.close(id, "expired", "Request deadline elapsed", "core");
      return this.stored(id);
    }
    return row;
  }
  cursor(row: RequestRow): string {
    return Buffer.from(JSON.stringify([row.created_at, row.id])).toString("base64url");
  }
  owned(id: string, owner: ClientIdentity): RequestRow {
    const row = this.get(id);
    if (!row || owner.workspaceId !== row.workspace_id || owner.clientId !== row.client_id || owner.host !== row.host)
      throw new AttentionError("not_found", "No request for this client", 404);
    return row;
  }
  list(owner?: ClientIdentity, before?: string): RequestRow[] {
    if (owner && owner.workspaceId !== this.config.attention.workspace_id) throw new AttentionError("not_found", "Unknown workspace", 404);
    this.sweep();
    let time: string | null = null; let id: string | null = null;
    if (before) {
      try {
        const cursor = JSON.parse(Buffer.from(before, "base64url").toString("utf8"));
        if (!Array.isArray(cursor) || cursor.length !== 2 || typeof cursor[0] !== "string" ||
          !/^\d{4}-\d\d-\d\dT/.test(cursor[0]) || !Number.isFinite(Date.parse(cursor[0])) ||
          typeof cursor[1] !== "string" || !/^req_[a-f0-9]{36}$/.test(cursor[1])) throw new Error("Invalid cursor");
        [time, id] = cursor;
      } catch { throw new AttentionError("invalid_cursor", "Use next_cursor from the preceding page", 400); }
    }
    return this.store.db.query(`SELECT * FROM attention_requests WHERE workspace_id=?
      AND (? IS NULL OR client_id=?) AND (? IS NULL OR host=?)
      AND (? IS NULL OR created_at < ? OR (created_at = ? AND id < ?))
      ORDER BY created_at DESC, id DESC LIMIT 101`)
      .all(this.config.attention.workspace_id, owner?.clientId ?? null, owner?.clientId ?? null,
        owner?.host ?? null, owner?.host ?? null, time, time, time, id) as RequestRow[];
  }
  /** The original hash never changes after enrichment, so offline replay remains valid. */
  raise(owner: ClientIdentity, key: string, packet: DecisionPacket): RequestRow {
    if (owner.workspaceId !== this.config.attention.workspace_id) throw new AttentionError("not_found", "Unknown workspace", 404);
    return this.store.db.transaction(() => {
      const id = stableId("req", { workspace: owner.workspaceId, client: owner.clientId, key });
      const initialHash = hash(packet);
      const existing = this.get(id);
      if (existing) {
        if (existing.host !== owner.host) throw new AttentionError("identity_changed", "This client identity belongs to a different host. Use a new client identity.");
        if (existing.initial_hash !== initialHash) throw new AttentionError("idempotency_conflict", "This key already identifies different work. Enrich the existing request or use a new key.");
        return existing;
      }
      const active = this.store.db.query("SELECT COUNT(*) AS n FROM attention_requests WHERE workspace_id=? AND client_id=? AND state IN ('preparing','needs_you','answered','received')")
        .get(owner.workspaceId, owner.clientId) as { n: number };
      if (active.n >= this.config.attention.max_active_per_client) throw new AttentionError("too_many_active_requests", "Read your existing requests; cancel superseded work or finish outstanding decisions before raising more.");
      const now = this.now();
      const due = packet.deadline_at ? new Date(packet.deadline_at).toISOString() : null;
      const prepareAt = Math.min(Date.parse(now) + this.config.attention.prepare_seconds * 1_000,
        due ? Date.parse(due) - 5_000 : Infinity);
      this.store.db.query(`INSERT INTO attention_requests (id, workspace_id, client_id, host, idempotency_key,
        initial_hash, packet_json, state, prepare_by, due_at, created_at, updated_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?, ?, ?, ?)`)
        .run(id, owner.workspaceId, owner.clientId, owner.host, key, initialHash, JSON.stringify(packet),
          new Date(prepareAt).toISOString(), due, now, now, now);
      this.store.audit(`client:${owner.clientId}`, "request.raised", "attention_request", id, { workspace: owner.workspaceId, host: owner.host });
      if (due && due <= now) this.close(id, "expired", "Deadline passed before CAR received the request", "core");
      else if (packet.urgency === "urgent" || assessPacket(packet).length === 0 || prepareAt <= Date.parse(now)) this.publish(id);
      return this.get(id)!;
    })();
  }
  enrich(owner: ClientIdentity, id: string, revision: number, packet: DecisionPacket): RequestRow {
    return this.store.db.transaction(() => {
      const row = this.owned(id, owner);
      // A lost HTTP response may be replayed after the enrichment was committed.
      // Exact replay returns the current state, never reapplies or rewinds context.
      if (row.revision === revision + 1 && hash(JSON.parse(row.packet_json)) === hash(packet)) return row;
      if (row.revision !== revision) throw new AttentionError("revision_conflict", "Read the latest request and merge your context");
      if (row.state !== "preparing") throw new AttentionError("packet_frozen", "A surfaced decision cannot be edited underneath the human. Cancel it and raise a replacement.");
      // Enrichment cannot move the human's deadline backwards or hide an urgent request.
      const old = JSON.parse(row.packet_json) as DecisionPacket;
      const changedDue = packet.deadline_at ? new Date(packet.deadline_at).toISOString() : null;
      if (changedDue !== row.due_at || packet.urgency !== old.urgency || packet.question !== old.question)
        throw new AttentionError("identity_changed", "Enrichment adds context; changing question, urgency or deadline requires a new request");
      const rounds = row.preparation_rounds + 1;
      this.store.db.query("UPDATE attention_requests SET packet_json=?, revision=revision+1, preparation_rounds=?, updated_at=?, last_seen_at=? WHERE id=?")
        .run(JSON.stringify(packet), rounds, this.now(), this.now(), id);
      this.store.audit(`client:${owner.clientId}`, "request.context_added", "attention_request", id, { revision: revision + 1, rounds });
      if (!assessPacket(packet).length || rounds >= this.config.attention.max_context_rounds || row.prepare_by <= this.now()) this.publish(id);
      return this.get(id)!;
    })();
  }
  /** Idempotent publication into the EXISTING incident/escalation/outbox model. */
  publish(id: string): RequestRow {
    return this.store.db.transaction(() => {
      const row = this.get(id);
      if (!row) throw new AttentionError("not_found", "Unknown request", 404);
      if (row.state !== "preparing") return row;
      if (row.due_at && row.due_at <= this.now()) { this.close(id, "expired", "Deadline passed during preparation", "core"); return this.get(id)!; }
      const packet = JSON.parse(row.packet_json) as DecisionPacket;
      const context = decisionContext(packet);
      const missing = assessPacket(packet);
      if (missing.length) context.push(`Context incomplete: ${missing.map((m) => m.field).join(", ")}. Surfaced now rather than hiding the blocker.`);
      const event: CarEvent = {
        contract: "car.event.v1", idempotency_key: `attention:${row.id}`, ts: this.now(),
        source: { vendor: "other", host: row.host, adapter: "attention-api" }, session: null,
        type: "attention.question", severity: packet.urgency === "urgent" ? "urgent" : "attention",
        requires_response: true, response_channel: null, title: packet.question,
        body: context.join("\n").slice(0, 16_000), payload: { attention_request_id: id, packet_revision: row.revision },
        ...(row.due_at ? { expires_at: row.due_at } : {}),
      };
      const eventResult = this.store.ingestEvent(event, { sourceId: `attention:${row.workspace_id}:${row.client_id}` });
      const incident = this.repo.openIncident({ carSessionId: null, openedByEvent: eventResult.event_id,
        dedupeClass: `request:${row.id}`, summary: packet.question });
      const escalationId = this.repo.createEscalation({ id: stableId("esc", { request: row.id }),
        originEventId: eventResult.event_id, incidentId: incident.id, severity: event.severity, question: packet.question });
      this.repo.setIncidentState(incident.id, "escalated", { summary: context.join("\n") });
      this.repo.recordDecision({ id: stableId("dec", { request: row.id, revision: row.revision }), incidentId: incident.id,
        decidedBy: "rules", disposition: "escalate", rationale: missing.length ? "Preparation budget exhausted; missing context is explicit" : "Complete decision packet; no model call required" });
      this.store.setEventTriageState(eventResult.event_id, "escalated", incident.id);
      this.store.db.query("UPDATE attention_requests SET state='needs_you', event_id=?, incident_id=?, escalation_id=?, updated_at=? WHERE id=?")
        .run(eventResult.event_id, incident.id, escalationId, this.now(), id);
      if (this.config.telegram.enabled) this.channel.sendEscalation({ escalationId, incidentId: incident.id,
        carSessionId: null, severity: event.severity, question: packet.question, contextLines: context });
      this.store.audit("core", "request.surfaced", "attention_request", id, { revision: row.revision, missing_context: missing.map((m) => m.field) });
      return this.get(id)!;
    })();
  }
  answer(id: string, revision: number, actor: string, answer: { text?: string; option_id?: string }): ReplyRow {
    if ((answer.text !== undefined) === (answer.option_id !== undefined))
      throw new AttentionError("invalid_answer", "Choose one option OR write one answer", 400);
    const row = this.get(id);
    if (!row?.escalation_id) throw new AttentionError("not_ready", "This request is not awaiting a human answer");
    const packet = JSON.parse(row.packet_json) as DecisionPacket;
    const option = answer.option_id ? packet.options.find((o) => o.id === answer.option_id) : null;
    if (answer.option_id && !option) throw new AttentionError("unknown_option", "That option does not belong to this revision", 400);
    return recordAnswer(this.store, { escalationId: row.escalation_id, expectedRevision: revision, actor,
      payload: { text: option?.answer ?? answer.text ?? "" } });
  }
  acknowledge(owner: ClientIdentity, id: string, answerId: string, outcome: "received" | "resolved", note?: string): RequestRow {
    return this.store.db.transaction(() => {
      const row = this.owned(id, owner);
      const reply = this.store.db.query("SELECT * FROM human_replies WHERE request_id=? AND id=?").get(id, answerId) as ReplyRow | null;
      if (!reply) throw new AttentionError("answer_mismatch", "Acknowledge the exact answer_id returned by CAR");
      if (row.state === "resolved" && reply.state === "resolved") return row;
      if (row.state === "received" && reply.state === "acknowledged" && outcome === "received") return row;
      if (terminalRequest(row.state)) throw new AttentionError("request_closed", `Request is ${row.state}; do not apply a stale answer`);
      if (row.state !== "answered" && row.state !== "received") throw new AttentionError("not_answered", "No human answer is available");
      if (outcome === "resolved" && row.state !== "received") throw new AttentionError("receipt_required", "Persist and acknowledge received before reporting the blocker resolved");
      if (row.state === "answered" && row.due_at && row.due_at <= this.now()) throw new AttentionError("answer_expired", "Answer expired before receipt; request a fresh decision");
      const now = this.now();
      const resolved = outcome === "resolved";
      this.store.db.query(`UPDATE human_replies SET revision=revision+1, state=?, acknowledged_at=COALESCE(acknowledged_at,?),
        resolved_at=?, updated_at=? WHERE id=?`).run(resolved ? "resolved" : "acknowledged", now, resolved ? now : null, now, answerId);
      this.store.db.query("UPDATE attention_requests SET state=?, updated_at=?, last_seen_at=?, closed_at=?, close_reason=? WHERE id=?")
        .run(resolved ? "resolved" : "received", now, now, resolved ? now : null, resolved ? note ?? "Source confirmed unblocked" : null, id);
      if (row.event_id) this.store.db.query("UPDATE events SET obligation_state=? WHERE id=?").run(resolved ? "resolved" : "acknowledged", row.event_id);
      if (resolved && row.incident_id) this.repo.setIncidentState(row.incident_id, "resolved");
      this.store.audit(`client:${owner.clientId}`, resolved ? "request.resolved" : "reply.acknowledged", "attention_request", id, { answer_id: answerId, note: note ?? null });
      return this.get(id)!;
    })();
  }
  cancel(owner: ClientIdentity, id: string, revision: number, reason: string): RequestRow {
    const row = this.owned(id, owner);
    return this.withdraw(row.id, revision, reason, `client:${owner.clientId}`);
  }
  /** Human-only alternative when the source disappeared. This is not execution rollback. */
  withdraw(id: string, revision: number, reason: string, actor: string): RequestRow {
    if (!reason.trim() || reason.length > 2_000) throw new AttentionError("reason_required", "Explain why this request is obsolete", 400);
    return this.store.db.transaction(() => {
      const row = this.get(id);
      if (!row) throw new AttentionError("not_found", "Unknown request", 404);
      if (row.revision !== revision) throw new AttentionError("revision_conflict", "Read the current request before withdrawing it");
      if (row.state === "cancelled") return row;
      if (terminalRequest(row.state)) throw new AttentionError("request_closed", `Request is already ${row.state}; its outcome is not changed`);
      this.close(id, "cancelled", reason.trim(), actor);
      return this.get(id)!;
    })();
  }
  /** A missed decision is acknowledged, never retroactively relabelled as success. */
  reviewExpiry(id: string, revision: number, actor: string, note: string): RequestRow {
    if (!note.trim() || note.length > 2_000) throw new AttentionError("reason_required", "Record how the missed decision was handled", 400);
    return this.store.db.transaction(() => {
      const row = this.get(id);
      if (!row) throw new AttentionError("not_found", "Unknown request", 404);
      if (row.revision !== revision || row.state !== "expired") throw new AttentionError("request_changed", "Review the current expired request");
      if (row.reviewed_at) return row;
      this.store.db.query("UPDATE attention_requests SET reviewed_at=?, review_note=? WHERE id=?")
        .run(this.now(), note.trim(), id);
      this.store.recordHumanFact({ sourceId: "core:missed-decisions", idempotencyKey: `review:${id}`, kind: "feedback",
        targetType: "attention_request", targetId: id, actorId: actor, body: { outcome: "expired", note: note.trim() } });
      this.store.audit(actor, "request.expiry_reviewed", "attention_request", id, { note: note.trim() });
      return this.get(id)!;
    })();
  }
  private close(id: string, state: "cancelled" | "expired", reason: string, actor: string): void {
    this.store.db.transaction(() => {
      const row = this.stored(id);
      if (!row || terminalRequest(row.state)) return;
      const now = this.now();
      this.store.db.query("UPDATE attention_requests SET state=?, updated_at=?, closed_at=?, close_reason=? WHERE id=?").run(state, now, now, reason, id);
      if (row.event_id) this.store.db.query("UPDATE events SET obligation_state=? WHERE id=?").run(state, row.event_id);
      if (row.escalation_id) this.store.db.query("UPDATE escalations SET state=? WHERE id=? AND state IN ('pending','snoozed')")
        .run(state === "cancelled" ? "superseded" : "expired", row.escalation_id);
      this.store.db.query("UPDATE human_replies SET revision=revision+1, state=?, updated_at=? WHERE request_id=? AND state != 'resolved'").run(state, now, id);
      if (row.incident_id) this.repo.setIncidentState(row.incident_id, state === "cancelled" ? "resolved" : "expired", { summary: reason });
      this.store.audit(actor, `request.${state}`, "attention_request", id, { reason });
      if (state === "expired" && this.config.telegram.enabled) {
        this.channel.sendNotify(`A CAR decision deadline passed without confirmed receipt. No approval is implied. Review the missed decision in Needs you (${id}).`);
      }
    })();
  }
  /**
   * Native obligations do not have an attention request row, so they cannot
   * rely on the guided-request sweep above. Expire them in one transaction at
   * the durable source-of-truth boundary; the conditional update makes a
   * repeated sweep a no-op and keeps late reply workers from sending them.
   */
  private sweepNative(): void {
    const now = this.now();
    this.store.db.transaction(() => {
      const rows = this.store.db.query(`SELECT e.id, e.incident_id, e.expires_at
        FROM events e
        WHERE e.requires_response=1 AND e.expires_at IS NOT NULL AND e.expires_at <= ?
          AND e.obligation_state NOT IN ('resolved','cancelled','expired')
          AND NOT EXISTS (SELECT 1 FROM attention_requests r WHERE r.event_id=e.id)
        ORDER BY e.expires_at ASC, e.id ASC`).all(now) as {
        id: string; incident_id: string | null; expires_at: string;
      }[];
      for (const row of rows) {
        const expired = this.store.db.query(`UPDATE events SET obligation_state='expired'
          WHERE id=? AND requires_response=1
            AND obligation_state NOT IN ('resolved','cancelled','expired')`).run(row.id);
        if (!expired.changes) continue;

        // A reply already being sent has an unknown remote outcome. Do not
        // relabel that uncertainty as a clean expiry or permit a blind retry.
        this.store.db.query(`UPDATE human_replies SET revision=revision+1,
            state=CASE WHEN state='delivering' THEN 'uncertain' ELSE 'expired' END,
            last_error=CASE WHEN state='delivering'
              THEN 'Request expired while delivery was in flight; remote acceptance is unknown. Check the source before retrying.'
              ELSE COALESCE(last_error, 'Request deadline elapsed; answer retained in history.') END,
            updated_at=?
          WHERE event_id=? AND state IN ('pending','staged','delivering')`).run(now, row.id);
        this.store.db.query("UPDATE escalations SET state='expired' WHERE origin_event_id=? AND state IN ('pending','snoozed')")
          .run(row.id);
        this.store.audit("core", "obligation.expired", "event", row.id, { expires_at: row.expires_at, native: true });

        if (!row.incident_id) continue;
        const sibling = this.store.db.query(`SELECT 1 FROM events
          WHERE incident_id=? AND requires_response=1
            AND obligation_state NOT IN ('resolved','cancelled','expired') LIMIT 1`).get(row.incident_id);
        if (sibling) continue;
        const closed = this.store.db.query(`UPDATE incidents SET state='expired', closed_at=?, snooze_until=NULL,
            summary=CASE WHEN summary='' THEN 'Native response deadline elapsed' ELSE summary END
          WHERE id=? AND state IN ('open','escalated','snoozed')`).run(now, row.incident_id);
        if (closed.changes) this.store.audit("core", "incident.expired", "incident", row.incident_id, { event_id: row.id });
      }
    })();
  }
  summary(row: RequestRow) {
    const packet = JSON.parse(row.packet_json) as DecisionPacket;
    return { id: row.id, revision: row.revision, state: row.state, question: packet.question,
      project: packet.project ?? null, urgency: packet.urgency, due_at: row.due_at, updated_at: row.updated_at,
      review_required: row.state === "expired" && !row.reviewed_at,
      next_action: "Get this request for context instructions or its exact answer. Listing does not acknowledge receipt." };
  }
  view(row: RequestRow) {
    const packet = JSON.parse(row.packet_json) as DecisionPacket;
    const reply = this.store.db.query("SELECT * FROM human_replies WHERE request_id=?").get(row.id) as ReplyRow | null;
    const triage = this.store.db.query("SELECT state, proposal_json, error FROM attention_triage_runs WHERE request_id=? AND revision=?")
      .get(row.id, row.revision) as { state: string; proposal_json: string | null; error: string | null } | null;
    return { contract: "car.request.v1", acceptance: "server" as const, id: row.id, revision: row.revision, state: row.state, packet,
      preparation: { deadline: row.prepare_by, rounds_remaining: Math.max(0, this.config.attention.max_context_rounds - row.preparation_rounds),
        context_requests: assessPacket(packet), triage: triage ? { state: triage.state, proposal: triage.proposal_json ? JSON.parse(triage.proposal_json) : null, error: triage.error } : null },
      answer: reply ? { id: reply.id, payload: JSON.parse(reply.payload_json), delivery: reply.state,
        eligible_for_receipt: !terminalRequest(row.state) && (!row.due_at || row.state === "received" || row.due_at > this.now()) } : null,
      guidance: requestGuidance(row, reply?.id ?? null, this.now()),
      observation: { last_contact_at: row.last_seen_at, meaning: "Last source API contact, not proof of running work." },
      outcome_review: { required: row.state === "expired" && !row.reviewed_at, reviewed_at: row.reviewed_at, note: row.review_note },
      next_action: row.state === "preparing" ? "Add the requested context with expected_revision. Do not restart the request or involve the human yourself." :
        row.state === "needs_you" ? "Continue unrelated work; poll this request. Do not infer approval from silence." :
        row.state === "answered" ? "Persist the answer locally, acknowledge received, then apply only to this request. Confirm resolved when actually unblocked." :
        row.state === "received" ? "Confirm resolved when actually unblocked; receiving an answer alone does not resolve the obligation." : "No action. Do not apply answers from a closed request.",
      due_at: row.due_at, created_at: row.created_at, updated_at: row.updated_at, close_reason: row.close_reason };
  }
  sweep(): void {
    const now = this.now();
    const expired = this.store.db.query("SELECT id FROM attention_requests WHERE workspace_id=? AND state IN ('preparing','needs_you','answered') AND due_at IS NOT NULL AND due_at <= ?")
      .all(this.config.attention.workspace_id, now) as { id: string }[];
    for (const row of expired) this.close(row.id, "expired", "Request deadline elapsed", "core");
    this.sweepNative();
    const ready = this.store.db.query("SELECT id FROM attention_requests WHERE workspace_id=? AND state='preparing' AND prepare_by <= ?")
      .all(this.config.attention.workspace_id, now) as { id: string }[];
    for (const row of ready) this.publish(row.id);
  }
  loop(): Loop {
    let timer: ReturnType<typeof setInterval> | undefined;
    return { name: "attention-deadlines", start: () => { this.sweep(); timer = setInterval(() => {
      try { this.sweep(); } catch (e) { this.store.audit("core", "attention.sweep_failed", "worker", "attention", { error: String(e) }); }
    }, 500); }, stop: () => { if (timer) clearInterval(timer); timer = undefined; } };
  }
}
