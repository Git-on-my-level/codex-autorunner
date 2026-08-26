/**
 * Test helpers for the Telegram surface. No network, no grammY: the bot module
 * is only ever reachable through a dynamic import inside loop.start(), which
 * these tests never trigger with a token.
 */
import { Store } from "../../src/store/db.ts";
import type { MemoryWriter } from "../../src/ports.ts";
import type { MessageSpec, TelegramSendFn, TelegramTarget } from "../../src/surfaces/telegram/types.ts";
import { escalationId, incidentId, decisionId } from "../../src/contract/ids.ts";
import { CONTRACT_VERSION, type CarEvent } from "../../src/contract/events.ts";

export class FakeMemoryWriter implements MemoryWriter {
  added: { tier: string; kind: string; content: Record<string, unknown>; scope: Record<string, unknown>; id: string }[] = [];
  proposed: { kind: string; id: string }[] = [];
  outcomes: {
    decisionId: string;
    escalationId?: string;
    verdict: string;
    davidAction?: Record<string, unknown>;
  }[] = [];
  autonomy: { memoryId: string; autonomy: string; by: string }[] = [];
  private seq = 0;

  addFromDavid(
    tier: string,
    kind: string,
    content: Record<string, unknown>,
    scope: Record<string, unknown>,
  ): string {
    const id = `mem_fake_${++this.seq}`;
    this.added.push({ tier, kind, content, scope, id });
    return id;
  }
  propose(kind: string): string {
    const id = `mem_prop_${++this.seq}`;
    this.proposed.push({ kind, id });
    return id;
  }
  recordOutcome(input: {
    decisionId: string;
    escalationId?: string;
    verdict: "confirmed" | "overridden" | "corrected" | "flagged";
    davidAction?: Record<string, unknown>;
  }): void {
    this.outcomes.push(input);
  }
  setAutonomy(memoryId: string, autonomy: "none" | "suggest" | "granted", by: "david"): void {
    this.autonomy.push({ memoryId, autonomy, by });
  }
}

/**
 * The fake Telegram transport. The deliverer is a pure function over
 * (store, sendFn), so this is the entire surface a test needs — grammY is
 * never constructed and nothing touches the network.
 */
export class FakeSend {
  calls: { target: TelegramTarget; spec: MessageSpec }[] = [];
  /** When set, every send throws this message. */
  fail: string | null = null;
  nextMessageId = 1000;

  readonly fn: TelegramSendFn = async (target: TelegramTarget, spec: MessageSpec) => {
    this.calls.push({ target, spec });
    if (this.fail) throw new Error(this.fail);
    if (target.edit_message_id) return { message_id: target.edit_message_id };
    const messageId = String(++this.nextMessageId);
    if (target.create_topic) return { message_id: messageId, thread_id: `topic_${messageId}` };
    return { message_id: messageId };
  };

  get last(): { target: TelegramTarget; spec: MessageSpec } | undefined {
    return this.calls[this.calls.length - 1];
  }
}

export interface SeededEscalation {
  eventId: string;
  incidentId: string;
  escalationId: string;
  decisionId: string;
  carSessionId: string;
}

export interface SeedOptions {
  vendor?: string;
  host?: string;
  repo?: string;
  title?: string;
  question?: string;
  suggested?: Record<string, unknown> | null;
  eventType?: CarEvent["type"];
  severity?: CarEvent["severity"];
  dedupeClass?: string;
  responseChannel?: CarEvent["response_channel"];
  requiresResponse?: boolean;
}

/** Seed session → event → incident → decision → escalation, the real chain. */
export function seedEscalation(store: Store, opts: SeedOptions = {}): SeededEscalation {
  const now = store.clock.now().toISOString();
  const vendor = opts.vendor ?? "claude-code";
  const host = opts.host ?? "mac-studio";
  const event: CarEvent = {
    contract: CONTRACT_VERSION,
    idempotency_key: `seed:${Math.random()}`,
    ts: now,
    source: { vendor: vendor as never, host, adapter: "hook-http" },
    session: {
      vendor: vendor as never,
      native_id: `native-${Math.random().toString(36).slice(2, 8)}`,
      host,
      ...(opts.repo ? { repo: opts.repo } : {}),
      title: opts.title ?? "fix BLE reconnect",
    },
    type: opts.eventType ?? "attention.permission",
    severity: opts.severity ?? "attention",
    requires_response: opts.requiresResponse ?? true,
    response_channel: opts.responseChannel ?? { kind: "claude-hook-http", hint: { tool_use_id: "toolu_1" } },
    title: "Permission: git push --force",
    body: "remote diverged",
    payload: {},
  };
  const ingested = store.ingestEvent(event);
  const carSessionId = ingested.car_session_id!;

  const inc = incidentId();
  store.db
    .query(
      `INSERT INTO incidents (id, car_session_id, opened_by_event, state, summary, dedupe_class, opened_at)
       VALUES (?, ?, ?, 'escalated', ?, ?, ?)`,
    )
    .run(inc, carSessionId, ingested.event_id, "force-push to fix/telemetry-cliff", opts.dedupeClass ?? "force_push", now);
  store.setEventTriageState(ingested.event_id, "escalated", inc);

  const dec = decisionId();
  store.db
    .query(
      `INSERT INTO decisions (id, incident_id, decided_by, disposition, action_class, rationale, created_at)
       VALUES (?, ?, 'llm', 'escalate', 'approve_permission', ?, ?)`,
    )
    .run(dec, inc, "remote has CI-authored commits", now);

  const esc = escalationId();
  const suggested = opts.suggested === undefined ? { approval: false, label: "DENY — tell agent to rebase instead" } : opts.suggested;
  store.db
    .query(
      `INSERT INTO escalations (id, incident_id, severity, question, suggested_action_json, state, created_at)
       VALUES (?, ?, ?, ?, ?, 'pending', ?)`,
    )
    .run(
      esc,
      inc,
      opts.severity ?? "attention",
      opts.question ?? "force-push to fix/telemetry-cliff? Remote diverged.",
      suggested ? JSON.stringify(suggested) : null,
      now,
    );

  return { eventId: ingested.event_id, incidentId: inc, escalationId: esc, decisionId: dec, carSessionId };
}

/** Pretend the escalation card was already delivered as `messageId`. */
export function markDelivered(store: Store, seeded: SeededEscalation, messageId = "555"): void {
  store.db
    .query("UPDATE escalations SET telegram_message_id = ?, sent_at = ? WHERE id = ?")
    .run(messageId, store.clock.now().toISOString(), seeded.escalationId);
  store.db.query("UPDATE incidents SET telegram_message_id = ? WHERE id = ?").run(messageId, seeded.incidentId);
  store.kvSet(`tg.msg.${messageId}`, {
    escalation_id: seeded.escalationId,
    incident_id: seeded.incidentId,
    car_session_id: seeded.carSessionId,
  });
}

export function outboxRows(store: Store): {
  id: number;
  state: string;
  attempts: number;
  next_attempt_at: string;
  sent_message_id: string | null;
  target: TelegramTarget;
  body: MessageSpec;
}[] {
  const rows = store.db.query("SELECT * FROM outbox ORDER BY id ASC").all() as {
    id: number;
    state: string;
    attempts: number;
    next_attempt_at: string;
    sent_message_id: string | null;
    target_json: string;
    body_json: string;
  }[];
  return rows.map((r) => ({
    id: r.id,
    state: r.state,
    attempts: r.attempts,
    next_attempt_at: r.next_attempt_at,
    sent_message_id: r.sent_message_id,
    target: JSON.parse(r.target_json) as TelegramTarget,
    body: JSON.parse(r.body_json) as MessageSpec,
  }));
}

export function auditVerbs(store: Store): string[] {
  return (store.db.query("SELECT verb FROM audit ORDER BY id ASC").all() as { verb: string }[]).map(
    (r) => r.verb,
  );
}
