/**
 * Button taps, replies and commands.
 *
 * THE RULE (the v2 40k-LOC bot lesson): a handler only ever WRITES ROWS.
 * It never calls the Telegram API. Anything that must appear in the chat —
 * including editing an escalation card in place — is enqueued to the outbox and
 * delivered by outbox.ts. That makes every interaction testable with a store
 * and a fake clock, and makes the bot crash-only.
 */
import type { Store } from "../../store/db.ts";
import type { CarConfig } from "../../config/config.ts";
import type { ActionBus, MemoryWriter } from "../../ports.ts";
import { CONTRACT_VERSION, type CarEvent } from "../../contract/events.ts";
import { localAt, nextLocalAt, parseDuration } from "../../digest/time.ts";
import { enqueueMessage, tickerKey } from "./outbox.ts";
import {
  CB,
  alwaysKeyboard,
  encodeCallback,
  escalationKeyboard,
  decodeCallback,
  renderDigestMessage,
  renderEscalation,
  renderReplyPrompt,
  renderResolution,
  renderSnoozed,
  snoozeKeyboard,
  type CallbackOp,
} from "./render.ts";
import {
  getEscalation,
  getEscalationByMessage,
  getIncident,
  incidentResponseChannel,
  latestDecision,
  openingEventType,
  suggestedApproval,
  suggestedLabel,
  type EscalationRow,
  type IncidentRow,
} from "./rows.ts";
import { getSession, resolveTarget, sessionLabel } from "./targets.ts";
import type { MessageSpec, TelegramTarget } from "./types.ts";

export const KV_ESCALATE_ONLY = "escalate_only";
export const KV_TICKER = "telegram.ticker";

export interface HandlerDeps {
  store: Store;
  config: CarConfig;
  actions: ActionBus;
  memoryWriter: MemoryWriter;
  /** Host recorded on synthetic events this surface ingests (Telegram notes). */
  host: string;
}

/** What the bot layer should show as the callback-query toast. */
export interface CallbackAck {
  text: string;
  alert?: boolean;
}

export interface CallbackInput {
  data: string;
  /** Telegram user id / username of the tapper. Only David's taps grant autonomy. */
  from?: string;
  /** The message the button is attached to, so it can be edited in place. */
  messageId?: string;
  messageText?: string;
}

/* ------------------------------------------------------------- callbacks */

export async function handleCallback(deps: HandlerDeps, input: CallbackInput): Promise<CallbackAck> {
  const decoded = decodeCallback(input.data);
  if (!decoded) return { text: "unknown button" };
  const { op, id } = decoded;
  deps.store.audit("david", "telegram.callback", "callback", id, { op });

  switch (op) {
    case CB.approve:
      return answerEscalation(deps, id, true, input);
    case CB.deny:
      return answerEscalation(deps, id, false, input);
    case CB.reply:
      return promptReply(deps, id);
    case CB.snoozeMenu:
      return swapKeyboard(deps, id, input, snoozeKeyboard(id), "snooze how long?");
    case CB.backToMain:
      return swapKeyboard(deps, id, input, escalationKeyboard(id, approveDenyAllowed(deps, id)), "back");
    case CB.snooze1h:
      return snooze(deps, id, input, "1h");
    case CB.snoozeTonight:
      return snooze(deps, id, input, "tonight");
    case CB.snoozeDigest:
      return snooze(deps, id, input, "digest");
    case CB.alwaysMenu:
      return swapKeyboard(deps, id, input, alwaysKeyboard(id), "grant autonomy?");
    case CB.alwaysAll:
      return grantAlways(deps, id, false, input);
    case CB.alwaysRepo:
      return grantAlways(deps, id, true, input);
    case CB.alwaysKeep:
      return swapKeyboard(deps, id, input, escalationKeyboard(id, approveDenyAllowed(deps, id)), "keeping ask");
    case CB.digestUp:
      return digestFeedback(deps, id, "confirmed");
    case CB.digestDown:
      return digestFeedback(deps, id, "overridden");
    case CB.promoteYes:
      return promote(deps, id, false);
    case CB.promoteRepo:
      return promote(deps, id, true);
    case CB.promoteKeep:
      deps.store.audit("david", "memory.promotion_declined", "memory", id, {});
      return { text: "Keeping ask-first." };
    case CB.probe:
      return probeStuck(deps, id);
    case CB.escalateNow:
      return escalateStuck(deps, id);
    case CB.memoryReview:
      return { text: "Pending memories: review in the web UI (/memory)." };
    default:
      return { text: "unknown button" };
  }
}

function approveDenyAllowed(deps: HandlerDeps, escalationId: string): boolean {
  const esc = getEscalation(deps.store, escalationId);
  if (!esc) return true;
  const inc = getIncident(deps.store, esc.incident_id);
  if (!inc) return true;
  return openingEventType(deps.store, inc) !== "attention.question";
}

/* ------------------------------------------------------- approve / deny */

async function answerEscalation(
  deps: HandlerDeps,
  escalationId: string,
  approval: boolean,
  input: CallbackInput,
): Promise<CallbackAck> {
  const { store } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  if (esc.state === "answered") return { text: "Already answered." };

  const incident = getIncident(store, esc.incident_id);
  const now = store.clock.now().toISOString();
  const answer = { approval, by: "david" };

  store.db
    .query(
      "UPDATE escalations SET state = 'answered', answer_json = ?, answered_by = 'david', answered_at = ? WHERE id = ?",
    )
    .run(JSON.stringify(answer), now, escalationId);
  if (incident) {
    store.db
      .query("UPDATE incidents SET state = 'resolved', closed_at = ?, snooze_until = NULL WHERE id = ?")
      .run(now, incident.id);
  }
  store.audit("david", "escalation.answered", "escalation", escalationId, answer);

  // The tap IS the learning signal.
  recordTapOutcome(deps, esc, { approval });

  // Route the answer back to the agent.
  let delivery = "skipped";
  if (incident?.car_session_id) {
    delivery = await deps.actions.deliver(
      incident.car_session_id,
      incidentResponseChannel(store, incident),
      { approval },
    );
    store.audit("david", "escalation.delivered", "escalation", escalationId, { delivery });
  }

  const verb = approval ? "✅ approved" : "❌ denied";
  editEscalationCard(deps, esc, incident, renderResolution(cardText(deps, esc, input), verb).text, []);

  if (delivery === "failed") {
    // Non-negotiable #6: a reply that cannot reach its agent is loud.
    reEscalateDeliveryFailure(deps, esc, incident);
    return { text: `${verb}, but delivery FAILED — re-escalated.`, alert: true };
  }
  return { text: `${verb}${delivery === "degraded" ? " (staged, not live-delivered)" : ""}` };
}

function recordTapOutcome(
  deps: HandlerDeps,
  esc: EscalationRow,
  davidAction: Record<string, unknown>,
): void {
  const decision = latestDecision(deps.store, esc.incident_id);
  if (!decision) {
    deps.store.audit("david", "outcome.skipped_no_decision", "escalation", esc.id, {});
    return;
  }
  const suggested = suggestedApproval(esc.suggested_action_json);
  const tapped = typeof davidAction.approval === "boolean" ? (davidAction.approval as boolean) : null;
  // confirmed when the tap matches CAR's suggestion, overridden when it contradicts it.
  // When CAR had no opinion (or David typed free text) there is nothing to confirm or
  // override, so the outcome is recorded neutrally rather than punishing a rule.
  const verdict =
    tapped === null ? "corrected" : suggested === null ? "flagged" : suggested === tapped ? "confirmed" : "overridden";
  deps.memoryWriter.recordOutcome({
    decisionId: decision.id,
    escalationId: esc.id,
    verdict,
    davidAction,
  });
}

function reEscalateDeliveryFailure(deps: HandlerDeps, esc: EscalationRow, incident: IncidentRow | null): void {
  const { store, config } = deps;
  if (incident) {
    store.db.query("UPDATE incidents SET state = 'open', closed_at = NULL WHERE id = ?").run(incident.id);
  }
  const target = resolveTarget(store, config, "notify", incident?.car_session_id ?? null);
  target.incident_id = incident?.id ?? null;
  target.escalation_id = esc.id;
  enqueueMessage(store, target, {
    text: `⚠️ Your answer could NOT be delivered to the agent (${esc.question.slice(0, 120)}). The incident is reopened.`,
  });
  store.audit("daemon", "escalation.delivery_failed", "escalation", esc.id, {});
}

/* ------------------------------------------------------------ 💬 reply */

function promptReply(deps: HandlerDeps, escalationId: string): CallbackAck {
  const { store, config } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  const incident = getIncident(store, esc.incident_id);
  const label = incident?.car_session_id
    ? sessionLabel(getSession(store, incident.car_session_id)) ?? null
    : null;
  const target = resolveTarget(store, config, "notify", incident?.car_session_id ?? null);
  target.escalation_id = escalationId;
  target.incident_id = esc.incident_id;
  enqueueMessage(store, target, renderReplyPrompt(label));
  return { text: "Reply to the prompt I just sent." };
}

/* ----------------------------------------------------------- 😴 snooze */

function snooze(
  deps: HandlerDeps,
  escalationId: string,
  input: CallbackInput,
  mode: "1h" | "tonight" | "digest",
): CallbackAck {
  const { store, config } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  const now = store.clock.now();
  const until =
    mode === "1h"
      ? new Date(now.getTime() + 3_600_000)
      : mode === "tonight"
        ? nextLocalOrToday(now, "22:00")
        : nextLocalAt(now, config.telegram.digest_time);

  const incident = getIncident(store, esc.incident_id);
  store.db
    .query("UPDATE escalations SET state = 'snoozed' WHERE id = ?")
    .run(escalationId);
  if (incident) {
    store.db
      .query("UPDATE incidents SET state = 'snoozed', snooze_until = ? WHERE id = ?")
      .run(until.toISOString(), incident.id);
  }
  const label = mode === "1h" ? "in 1h" : mode === "tonight" ? "tonight" : "the next digest";
  store.audit("david", "incident.snoozed", "incident", incident?.id ?? esc.incident_id, {
    until: until.toISOString(),
    mode,
  });
  editEscalationCard(deps, esc, incident, renderSnoozed(cardText(deps, esc, input), label).text, []);
  return { text: `😴 snoozed until ${label}` };
}

function nextLocalOrToday(now: Date, hhmm: string): Date {
  const today = localAt(now, hhmm);
  return today.getTime() > now.getTime() ? today : nextLocalAt(now, hhmm);
}

/* ------------------------------------------------------- 🧠 Always… */

/**
 * Create (or reuse) a rule-tier memory for this class of escalation and grant it
 * autonomy. Non-negotiable #4: ONLY this path — David's explicit tap — ever
 * calls setAutonomy(..., 'granted', 'david').
 */
function grantAlways(
  deps: HandlerDeps,
  escalationId: string,
  repoOnly: boolean,
  input: CallbackInput,
): CallbackAck {
  const { store } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  const incident = getIncident(store, esc.incident_id);
  const session = incident?.car_session_id ? getSession(store, incident.car_session_id) : null;
  const eventType = incident ? openingEventType(store, incident) : null;

  const scope: Record<string, unknown> = {};
  if (session?.vendor) scope.vendor = session.vendor;
  if (eventType) scope.event_type = eventType;
  if (incident?.dedupe_class) scope.dedupe_class = incident.dedupe_class;
  if (repoOnly) {
    const repo = session?.repo ?? session?.cwd ?? null;
    if (repo) scope.repo = repo;
  }

  const approval = suggestedApproval(esc.suggested_action_json);
  const content: Record<string, unknown> = {
    match: incident?.dedupe_class ?? eventType ?? esc.question.slice(0, 120),
    disposition: "auto_resolve",
    action_class: approval === false ? "deny_permission" : "approve_permission",
    args_template: approval === null ? {} : { approval },
    origin: "telegram_always_button",
    question: esc.question.slice(0, 200),
  };

  const existing = findRule(store, String(content.match), scope);
  const memoryId = existing ?? deps.memoryWriter.addFromDavid("rule", "autonomy", content, scope);
  // Explicit tap by David — the only grant path in the system.
  deps.memoryWriter.setAutonomy(memoryId, "granted", "david");
  store.audit("david", "memory.autonomy_granted_from_escalation", "memory", memoryId, {
    escalation_id: escalationId,
    repo_only: repoOnly,
    reused: Boolean(existing),
  });

  editEscalationCard(
    deps,
    esc,
    incident,
    `${cardText(deps, esc, input)}\n🧠 Always${repoOnly ? " (this repo)" : ""}: granted.`,
    escalationKeyboard(escalationId, approveDenyAllowed(deps, escalationId)),
  );
  return { text: `🧠 Granted${repoOnly ? " for this repo" : ""}. Still needs your call on this one.` };
}

function findRule(store: Store, match: string, scope: Record<string, unknown>): string | null {
  const row = store.db
    .query(
      `SELECT id FROM memories
       WHERE tier = 'rule' AND status = 'active'
         AND json_extract(content_json, '$.match') = ?
         AND scope_json = ?
       ORDER BY created_at DESC LIMIT 1`,
    )
    .get(match, JSON.stringify(scope)) as { id: string } | null;
  return row?.id ?? null;
}

/* ------------------------------------------- digest feedback / promotion */

function digestFeedback(deps: HandlerDeps, decisionId: string, verdict: "confirmed" | "overridden"): CallbackAck {
  const row = deps.store.db.query("SELECT id FROM decisions WHERE id = ?").get(decisionId) as { id: string } | null;
  if (!row) return { text: "That decision is gone." };
  deps.memoryWriter.recordOutcome({ decisionId, verdict, davidAction: { via: "digest" } });
  deps.store.audit("david", "digest.feedback", "decision", decisionId, { verdict });
  return { text: verdict === "confirmed" ? "👍 noted" : "👎 noted — that rule is demoted." };
}

/**
 * Digest promotion offer: "Auto-approve these? [Yes] [Yes, this repo only] [Keep asking]".
 * Again, only this tap grants autonomy.
 */
function promote(deps: HandlerDeps, memoryId: string, repoOnly: boolean): CallbackAck {
  const { store } = deps;
  const row = store.db
    .query("SELECT id, tier, kind, content_json, scope_json FROM memories WHERE id = ?")
    .get(memoryId) as
    | { id: string; tier: string; kind: string; content_json: string; scope_json: string }
    | null;
  if (!row) return { text: "That proposal is gone." };

  if (!repoOnly) {
    deps.memoryWriter.setAutonomy(memoryId, "granted", "david");
    store.audit("david", "memory.promoted", "memory", memoryId, { repo_only: false });
    return { text: "🧠 Granted — CAR will act on this without asking." };
  }

  // Narrower grant: a repo-scoped copy is granted, the broad rule stays at suggest.
  const scope = safeParse(row.scope_json);
  const repo = inferRepoForMemory(store, memoryId) ?? (scope.repo as string | undefined);
  if (!repo) {
    deps.memoryWriter.setAutonomy(memoryId, "granted", "david");
    store.audit("david", "memory.promoted", "memory", memoryId, { repo_only: true, repo: null });
    return { text: "🧠 Granted (no repo scope available, granted as-is)." };
  }
  const narrowed = deps.memoryWriter.addFromDavid(
    row.tier,
    row.kind,
    { ...safeParse(row.content_json), narrowed_from: memoryId },
    { ...scope, repo },
  );
  deps.memoryWriter.setAutonomy(narrowed, "granted", "david");
  store.audit("david", "memory.promoted_repo_scoped", "memory", narrowed, { from: memoryId, repo });
  return { text: `🧠 Granted for ${repo} only.` };
}

function inferRepoForMemory(store: Store, memoryId: string): string | null {
  const row = store.db
    .query("SELECT json_extract(scope_json, '$.repo') AS repo FROM memories WHERE id = ?")
    .get(memoryId) as { repo: string | null } | null;
  return row?.repo ?? null;
}

function safeParse(json: string): Record<string, unknown> {
  try {
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/* ------------------------------------------------- digest stuck actions */

/**
 * The digest's [🔍 probe] button. Read-only templates only; the executor
 * enforces that independently. The probe runs detached and reports its result
 * back into the thread as a normal outbox row — the tap itself just writes.
 */
function probeStuck(deps: HandlerDeps, carSessionId: string): CallbackAck {
  const { store, config } = deps;
  const session = getSession(store, carSessionId);
  if (!session) return { text: "Session not found." };

  const probe = session.cwd
    ? { templateId: "git.status", args: { repo: session.cwd } as Record<string, unknown> }
    : { templateId: "agentctl.recent", args: { limit: 10 } as Record<string, unknown> };
  store.audit("david", "digest.probe_requested", "session", carSessionId, probe);

  void deps.actions
    .runTemplate(probe.templateId, probe.args, { decisionId: `telegram:${carSessionId}`, mutating: false })
    .then((result) => {
      const target = resolveTarget(store, config, "notify", carSessionId);
      const head = `🔍 ${probe.templateId} · ${sessionLabel(session) ?? carSessionId}`;
      const body = (result.output || "(no output)").slice(0, 1500);
      enqueueMessage(store, target, { text: `${head}\n${result.ok ? "" : "⚠️ probe failed\n"}${body}` });
    })
    .catch((err: unknown) => {
      store.audit("daemon", "digest.probe_error", "session", carSessionId, { error: String(err) });
    });

  return { text: "🔍 probing…" };
}

function escalateStuck(deps: HandlerDeps, carSessionId: string): CallbackAck {
  const { store, config } = deps;
  const session = getSession(store, carSessionId);
  if (!session) return { text: "Session not found." };
  store.db
    .query("UPDATE incidents SET state = 'open', snooze_until = NULL WHERE car_session_id = ? AND state = 'snoozed'")
    .run(carSessionId);
  store.db
    .query(
      `UPDATE escalations SET state = 'pending'
       WHERE state = 'snoozed'
         AND incident_id IN (SELECT id FROM incidents WHERE car_session_id = ?)`,
    )
    .run(carSessionId);
  const target = resolveTarget(store, config, "notify", carSessionId);
  enqueueMessage(store, target, {
    text: `🔺 Escalated by you: ${sessionLabel(session) ?? carSessionId} — open incidents reopened.`,
  });
  store.audit("david", "digest.escalate_requested", "session", carSessionId, {});
  return { text: "Escalated." };
}

/* -------------------------------------------------- edit-in-place plumbing */

/** The card text to rewrite: what Telegram gave us, else re-render from the DB. */
function cardText(deps: HandlerDeps, esc: EscalationRow, input: CallbackInput): string {
  if (input.messageText) return input.messageText;
  const incident = getIncident(deps.store, esc.incident_id);
  const session = incident?.car_session_id ? getSession(deps.store, incident.car_session_id) : null;
  return renderEscalation({
    escalationId: esc.id,
    incidentId: esc.incident_id,
    carSessionId: incident?.car_session_id ?? null,
    severity: esc.severity as never,
    question: esc.question,
    contextLines: incident?.summary ? [incident.summary] : [],
    suggestedActionLabel: suggestedLabel(esc.suggested_action_json),
    sessionLabel: sessionLabel(session),
  }).text;
}

function editEscalationCard(
  deps: HandlerDeps,
  esc: EscalationRow,
  incident: IncidentRow | null,
  text: string,
  keyboard: MessageSpec["inline_keyboard"],
): void {
  const messageId = esc.telegram_message_id ?? incident?.telegram_message_id ?? null;
  if (!messageId) return; // not delivered yet — the deliverer will send the fresh state
  const target: TelegramTarget = {
    kind: "edit",
    chat_id: deps.config.telegram.chat_id,
    edit_message_id: messageId,
    escalation_id: esc.id,
    incident_id: esc.incident_id,
    car_session_id: incident?.car_session_id ?? null,
  };
  const spec: MessageSpec = { text };
  if (keyboard && keyboard.length) spec.inline_keyboard = keyboard;
  enqueueMessage(deps.store, target, spec);
}

function swapKeyboard(
  deps: HandlerDeps,
  escalationId: string,
  input: CallbackInput,
  keyboard: NonNullable<MessageSpec["inline_keyboard"]>,
  toast: string,
): CallbackAck {
  const esc = getEscalation(deps.store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  const incident = getIncident(deps.store, esc.incident_id);
  editEscalationCard(deps, esc, incident, cardText(deps, esc, input), keyboard);
  return { text: toast };
}

/* -------------------------------------------------------- inbound replies */

export interface IncomingMessage {
  text: string;
  messageId: string;
  replyToMessageId?: string | null;
  threadId?: string | null;
  from?: string;
}

export type RouteOutcome =
  | { kind: "delivered" | "degraded" | "queued" | "failed"; carSessionId: string; escalationId?: string }
  | { kind: "note"; eventId: string }
  | { kind: "ignored"; reason: string };

/**
 * A plain reply to a session anchor / topic routes verbatim to the agent
 * (the v2 reply inbox, generalized). Anything unrouteable becomes a `note`
 * event — DESIGN §2's telegram adapter.
 */
export async function routeIncomingMessage(
  deps: HandlerDeps,
  msg: IncomingMessage,
): Promise<RouteOutcome> {
  const { store } = deps;
  const text = msg.text.trim();
  if (!text) return { kind: "ignored", reason: "empty" };

  const route = resolveRoute(deps, msg);
  if (!route) return { kind: "note", eventId: ingestNote(deps, msg) };

  const incident = route.incidentId ? getIncident(store, route.incidentId) : null;
  const carSessionId = route.carSessionId ?? incident?.car_session_id ?? null;
  if (!carSessionId) return { kind: "note", eventId: ingestNote(deps, msg) };

  const esc = route.escalationId ? getEscalation(store, route.escalationId) : null;
  if (esc && esc.state === "pending") {
    const now = store.clock.now().toISOString();
    store.db
      .query(
        "UPDATE escalations SET state = 'answered', answer_json = ?, answered_by = 'david', answered_at = ? WHERE id = ?",
      )
      .run(JSON.stringify({ text }), now, esc.id);
    if (incident) {
      store.db.query("UPDATE incidents SET state = 'resolved', closed_at = ? WHERE id = ?").run(now, incident.id);
    }
    // Free text instead of a tap: recorded as a correction, never as a confirm.
    recordTapOutcome(deps, esc, { text });
    store.audit("david", "escalation.answered_text", "escalation", esc.id, { chars: text.length });
  }

  const channel = incident ? incidentResponseChannel(store, incident) : null;
  const result = await deps.actions.deliver(carSessionId, channel, { text });
  store.audit("david", "reply.delivered", "session", carSessionId, {
    result,
    escalation_id: esc?.id ?? null,
  });

  if (result === "failed") {
    const target = resolveTarget(store, deps.config, "notify", carSessionId);
    enqueueMessage(store, target, { text: `⚠️ Could not deliver your reply to ${carSessionId}. Nothing was dropped — retry or use the web UI.` });
  } else if (result === "degraded") {
    const target = resolveTarget(store, deps.config, "notify", carSessionId);
    enqueueMessage(store, target, { text: "📝 Reply staged for the agent (no live channel) — it lands on the next hook fire." });
  }

  const outcome: RouteOutcome = { kind: result, carSessionId };
  if (esc) outcome.escalationId = esc.id;
  return outcome;
}

function resolveRoute(
  deps: HandlerDeps,
  msg: IncomingMessage,
): { escalationId?: string; incidentId?: string; carSessionId?: string } | null {
  const { store } = deps;
  if (msg.replyToMessageId) {
    const mapped = store.kvGet<{
      escalation_id: string | null;
      incident_id: string | null;
      car_session_id: string | null;
    }>(`tg.msg.${msg.replyToMessageId}`);
    if (mapped) {
      const out: { escalationId?: string; incidentId?: string; carSessionId?: string } = {};
      if (mapped.escalation_id) out.escalationId = mapped.escalation_id;
      if (mapped.incident_id) out.incidentId = mapped.incident_id;
      if (mapped.car_session_id) out.carSessionId = mapped.car_session_id;
      if (Object.keys(out).length) return out;
    }
    const esc = getEscalationByMessage(store, msg.replyToMessageId);
    if (esc) return { escalationId: esc.id, incidentId: esc.incident_id };
    const inc = store.db
      .query("SELECT id, car_session_id FROM incidents WHERE telegram_message_id = ? ORDER BY opened_at DESC LIMIT 1")
      .get(msg.replyToMessageId) as { id: string; car_session_id: string | null } | null;
    if (inc) {
      const out: { incidentId: string; carSessionId?: string } = { incidentId: inc.id };
      if (inc.car_session_id) out.carSessionId = inc.car_session_id;
      return out;
    }
  }
  if (msg.threadId) {
    const sess = store.db
      .query("SELECT car_session_id FROM sessions WHERE telegram_thread_id = ? LIMIT 1")
      .get(msg.threadId) as { car_session_id: string } | null;
    if (sess) return { carSessionId: sess.car_session_id };
  }
  return null;
}

/** Unrouteable Telegram text becomes a first-class `note` event. */
function ingestNote(deps: HandlerDeps, msg: IncomingMessage): string {
  const now = deps.store.clock.now();
  const event: CarEvent = {
    contract: CONTRACT_VERSION,
    idempotency_key: `telegram:note:${msg.messageId}`,
    ts: now.toISOString(),
    source: { vendor: "other", host: deps.host, adapter: "telegram" },
    session: null,
    type: "note",
    severity: "info",
    requires_response: false,
    response_channel: null,
    title: msg.text.slice(0, 120),
    body: msg.text.slice(0, 4000),
    payload: { from: msg.from ?? "david", telegram_message_id: msg.messageId },
  };
  const res = deps.store.ingestEvent(event);
  return res.event_id;
}

/* ---------------------------------------------------------------- commands */

export interface CommandInput {
  command: string;
  args: string;
  from?: string;
}

export async function handleCommand(deps: HandlerDeps, input: CommandInput): Promise<string> {
  const { store, config } = deps;
  const cmd = input.command.replace(/^\//, "").split("@")[0]!.toLowerCase();
  store.audit("david", "telegram.command", "command", cmd, { args: input.args.slice(0, 200) });

  switch (cmd) {
    case "status":
      return statusText(deps);
    case "digest":
      return sendLatestDigest(deps);
    case "remember": {
      const text = input.args.trim();
      if (!text) return "Usage: /remember <text>";
      const id = deps.memoryWriter.addFromDavid("note", "fact", { text }, {});
      return `🧠 Remembered (${id}).`;
    }
    case "mute":
      return muteSession(deps, input.args);
    case "panic":
      return await panic(deps, input.args);
    case "ticker": {
      const arg = input.args.trim().toLowerCase();
      if (arg !== "on" && arg !== "off") return "Usage: /ticker on|off";
      store.kvSet(KV_TICKER, arg === "on");
      store.audit("david", "telegram.ticker", "kv", KV_TICKER, { enabled: arg === "on" });
      return `Ticker ${arg}.`;
    }
    case "policy": {
      const escalateOnly = escalateOnlyMode(store);
      return [
        "⚖️ Policy",
        `mode: ${escalateOnly ? "ESCALATE-ONLY" : "normal"}`,
        `file: ${config.state_dir}/policy.toml`,
        "Edit via the web UI; the daemon hot-reloads.",
      ].join("\n");
    }
    case "help":
      return [
        "/status — what needs you",
        "/digest — resend the latest digest",
        "/remember <text> — store a note",
        "/mute <session> <2h|1d> — silence a session",
        "/panic [off] — escalate-only mode",
        "/ticker on|off — per-session status lines",
        "/policy — current mode",
      ].join("\n");
    default:
      return `Unknown command /${cmd}. Try /help.`;
  }
}

function statusText(deps: HandlerDeps): string {
  const { store } = deps;
  const one = <T>(sql: string, ...params: unknown[]): T =>
    store.db.query(sql).get(...(params as never[])) as T;

  const incidents = one<{ open: number; escalated: number; snoozed: number }>(
    `SELECT
       SUM(state = 'open') AS open,
       SUM(state = 'escalated') AS escalated,
       SUM(state = 'snoozed') AS snoozed
     FROM incidents`,
  );
  const escalations = one<{ pending: number }>(
    "SELECT COUNT(*) AS pending FROM escalations WHERE state = 'pending'",
  );
  const sessions = one<{ active: number; muted: number }>(
    `SELECT SUM(state = 'active') AS active,
            SUM(muted_until IS NOT NULL AND muted_until > ?) AS muted
     FROM sessions`,
    store.clock.now().toISOString(),
  );
  const outbox = one<{ pending: number; dead: number; deferred: number }>(
    `SELECT SUM(state = 'pending') AS pending,
            SUM(state = 'dead') AS dead,
            SUM(state = 'deferred') AS deferred
     FROM outbox`,
  );
  const spend = store.spendToday();
  const escalateOnly = escalateOnlyMode(store);

  return [
    "📊 CAR status",
    `🙋 escalations pending: ${escalations.pending ?? 0}`,
    `📁 incidents: ${incidents.open ?? 0} open · ${incidents.escalated ?? 0} escalated · ${incidents.snoozed ?? 0} snoozed`,
    `🧑‍💻 sessions: ${sessions.active ?? 0} active · ${sessions.muted ?? 0} muted`,
    `📮 outbox: ${outbox.pending ?? 0} pending · ${outbox.deferred ?? 0} held · ${outbox.dead ?? 0} dead`,
    `💸 triage spend today: $${(spend.cost_usd ?? 0).toFixed(2)} (${spend.calls ?? 0} calls)`,
    `⚙️ mode: ${escalateOnly ? "ESCALATE-ONLY" : "normal"}`,
  ].join("\n");
}

function sendLatestDigest(deps: HandlerDeps): string {
  const { store, config } = deps;
  const row = store.db
    .query("SELECT day, rendered_md FROM digests ORDER BY day DESC LIMIT 1")
    .get() as { day: string; rendered_md: string } | null;
  if (!row) return "No digest has been built yet — the next one lands at the scheduled time.";
  enqueueMessage(
    store,
    { kind: "digest", chat_id: config.telegram.chat_id },
    renderDigestMessage(row.rendered_md),
  );
  return `Resending the digest for ${row.day}.`;
}

function muteSession(deps: HandlerDeps, args: string): string {
  const { store } = deps;
  const [ref, dur] = args.trim().split(/\s+/);
  if (!ref || !dur) return "Usage: /mute <session> <2h|30m|1d>";
  const ms = parseDuration(dur);
  if (ms === null) return `Cannot parse duration "${dur}". Use 30m, 2h, 1d.`;
  const carSessionId = resolveSessionRef(store, ref);
  if (!carSessionId) return `No session matching "${ref}".`;
  const until = new Date(store.clock.now().getTime() + ms).toISOString();
  store.db.query("UPDATE sessions SET muted_until = ? WHERE car_session_id = ?").run(until, carSessionId);
  store.audit("david", "session.muted", "session", carSessionId, { until });
  return `🔇 Muted ${carSessionId} until ${until}.`;
}

/** Resolve a /mute argument: car session id (or prefix), native id, or title. */
export function resolveSessionRef(store: Store, ref: string): string | null {
  const exact = store.db
    .query("SELECT car_session_id FROM sessions WHERE car_session_id = ?")
    .get(ref) as { car_session_id: string } | null;
  if (exact) return exact.car_session_id;

  const byRef = store.db
    .query("SELECT car_session_id FROM session_refs WHERE native_id = ? LIMIT 1")
    .get(ref) as { car_session_id: string } | null;
  if (byRef) return byRef.car_session_id;

  const like = store.db
    .query(
      `SELECT car_session_id FROM sessions
       WHERE car_session_id LIKE ?1 || '%' OR title LIKE '%' || ?1 || '%' OR repo LIKE '%' || ?1 || '%'
       ORDER BY last_event_at DESC LIMIT 1`,
    )
    .get(ref) as { car_session_id: string } | null;
  return like?.car_session_id ?? null;
}

/**
 * The breaker itself belongs to the policy module. We call its free functions
 * when they exist and fall back to the raw kv flag otherwise, so /panic works
 * whether or not WS-B's engine has landed.
 *
 * NOTE the kv value must stay a plain boolean: policy checks
 * `kvGet<boolean>('escalate_only') === true`.
 */
async function policyBreaker(): Promise<{
  trip?: (store: Store, reason: string) => void;
  clear?: (store: Store) => void;
}> {
  try {
    const mod = (await import("../../policy/index.ts")) as Record<string, unknown>;
    return {
      ...(typeof mod.tripBreaker === "function"
        ? { trip: mod.tripBreaker as (s: Store, r: string) => void }
        : {}),
      ...(typeof mod.clearBreaker === "function" ? { clear: mod.clearBreaker as (s: Store) => void } : {}),
    };
  } catch {
    return {};
  }
}

/** /panic — flip to escalate-only and cancel in-flight actions. /panic off clears it. */
async function panic(deps: HandlerDeps, args: string): Promise<string> {
  const { store } = deps;
  const breaker = await policyBreaker();
  const off = args.trim().toLowerCase() === "off";

  if (off) {
    if (breaker.clear) breaker.clear(store);
    else store.kvSet(KV_ESCALATE_ONLY, false);
    store.audit("david", "panic.cleared", "kv", KV_ESCALATE_ONLY, { via_policy: Boolean(breaker.clear) });
    return "✅ Escalate-only cleared. CAR may act autonomously again.";
  }

  if (breaker.trip) breaker.trip(store, "telegram /panic");
  else store.kvSet(KV_ESCALATE_ONLY, true);
  store.db
    .query("UPDATE actions SET state = 'failed', result_json = ? WHERE state IN ('pending','running')")
    .run(JSON.stringify({ cancelled_by: "panic" }));
  store.audit("david", "panic.engaged", "kv", KV_ESCALATE_ONLY, { via_policy: Boolean(breaker.trip) });
  return "🛑 ESCALATE-ONLY. In-flight actions cancelled; CAR will ask before anything. /panic off to clear.";
}

/** Strict read: policy stores a plain boolean, so anything else means "off". */
export function escalateOnlyMode(store: Store): boolean {
  return store.kvGet<boolean>(KV_ESCALATE_ONLY) === true;
}

/* ------------------------------------------------------------ ticker line */

/** One edited-in-place status line per session, only when /ticker is on. */
export function updateTicker(deps: HandlerDeps, carSessionId: string, line: string): boolean {
  const { store, config } = deps;
  if (store.kvGet<boolean>(KV_TICKER) !== true) return false;
  const existing = store.kvGet<string>(tickerKey(carSessionId));
  const target = resolveTarget(store, config, "ticker", carSessionId);
  if (existing) target.edit_message_id = existing;
  enqueueMessage(store, target, { text: line, disable_notification: true });
  return true;
}

export { encodeCallback, CB };
export type { CallbackOp };
