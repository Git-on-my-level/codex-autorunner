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
import type { EffectType, SafetyKernel, VerifiedScope } from "../../safety/index.ts";
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
  /** Core is the sole authority for panic state and effect grants. */
  safety: SafetyKernel;
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
  /** Telegram numeric user id of the tapper; only configured actors may mutate CAR. */
  from?: string;
  /** The message the button is attached to, so it can be edited in place. */
  messageId?: string;
  messageText?: string;
}

/**
 * Telegram's chat id identifies a conversation, not the human who tapped a
 * button. Every inbound update must carry an explicit, configured user id.
 * These helpers are shared by the grammY seam and the row-only handlers so a
 * test or another adapter cannot accidentally bypass the same gate.
 */
export function isAllowedTelegramActor(config: CarConfig, actorId: string | number | undefined | null): boolean {
  if (actorId === undefined || actorId === null) return false;
  const normalized = String(actorId).trim();
  return normalized.length > 0 && config.telegram.allowed_user_ids.includes(normalized);
}

export function isAllowedTelegramUpdate(
  config: CarConfig,
  chatId: string | number | undefined | null,
  actorId: string | number | undefined | null,
): boolean {
  if (!config.telegram.chat_id) return false;
  if (chatId === undefined || chatId === null || String(chatId) !== config.telegram.chat_id) return false;
  return isAllowedTelegramActor(config, actorId);
}

function telegramActor(config: CarConfig, actorId: string | number | undefined | null): string | null {
  if (!isAllowedTelegramActor(config, actorId)) return null;
  return String(actorId).trim();
}

function rejectTelegramActor(store: Store, actorId: string | number | undefined | null, objectType: string, objectId: string): void {
  store.audit("telegram", "telegram.actor_rejected", objectType, objectId, {
    actor_id: actorId === undefined || actorId === null ? null : String(actorId),
  });
}

/** Escalation-card operations must come from the live, durably bound card. */
const ESCALATION_CARD_OPS = new Set<CallbackOp>([
  CB.approve,
  CB.deny,
  CB.reply,
  CB.snoozeMenu,
  CB.backToMain,
  CB.snooze1h,
  CB.snoozeTonight,
  CB.snoozeDigest,
  CB.alwaysMenu,
  CB.alwaysAll,
  CB.alwaysRepo,
  CB.alwaysKeep,
]);

function requirePendingEscalationBinding(
  deps: HandlerDeps,
  escalationId: string,
  input: CallbackInput,
): CallbackAck | null {
  const esc = getEscalation(deps.store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  if (esc.state === "answered") return { text: "Already answered." };
  if (esc.state !== "pending") return { text: "That card is stale — the escalation is already handled.", alert: true };
  const messageId = input.messageId;
  if (!messageId || esc.telegram_message_id !== messageId) {
    deps.store.audit("telegram", "telegram.stale_card_rejected", "escalation", escalationId, {
      reason: "message_binding_mismatch",
      message_id: messageId ?? null,
      bound_message_id: esc.telegram_message_id,
    });
    return { text: "That card is stale — please use the current escalation card.", alert: true };
  }

  // The scalar escalation column is paired with the durable message map. Both
  // must agree, including the incident and session lineage, before a callback
  // can mutate lifecycle state or create authority.
  const binding = deps.store.kvGet<{
    escalation_id: string | null;
    incident_id: string | null;
    car_session_id: string | null;
  }>(`tg.msg.${messageId}`);
  const incident = getIncident(deps.store, esc.incident_id);
  const expectedSession = incident?.car_session_id ?? null;
  if (
    !binding ||
    binding.escalation_id !== esc.id ||
    binding.incident_id !== esc.incident_id ||
    binding.car_session_id !== expectedSession
  ) {
    deps.store.audit("telegram", "telegram.stale_card_rejected", "escalation", escalationId, {
      reason: "durable_message_map_mismatch",
      message_id: messageId,
      binding: binding ?? null,
    });
    return { text: "That card is stale — please use the current escalation card.", alert: true };
  }
  return null;
}

/* ------------------------------------------------------------- callbacks */

export async function handleCallback(deps: HandlerDeps, input: CallbackInput): Promise<CallbackAck> {
  const decoded = decodeCallback(input.data);
  const actor = telegramActor(deps.config, input.from);
  if (!actor) {
    rejectTelegramActor(deps.store, input.from, "callback", decoded?.id ?? input.data.slice(0, 120));
    return { text: "not authorized", alert: true };
  }
  if (!decoded) return { text: "unknown button" };
  const { op, id } = decoded;
  deps.store.audit(actor, "telegram.callback", "callback", id, { op });

  if (ESCALATION_CARD_OPS.has(op)) {
    const rejected = requirePendingEscalationBinding(deps, id, input);
    if (rejected) return rejected;
  }

  switch (op) {
    case CB.approve:
      return answerEscalation(deps, id, true, input, actor);
    case CB.deny:
      return answerEscalation(deps, id, false, input, actor);
    case CB.reply:
      return promptReply(deps, id);
    case CB.snoozeMenu:
      return swapKeyboard(deps, id, input, snoozeKeyboard(id), "snooze how long?");
    case CB.backToMain:
      return swapKeyboard(deps, id, input, escalationKeyboard(id, approveDenyAllowed(deps, id)), "back");
    case CB.snooze1h:
      return snooze(deps, id, input, "1h", actor);
    case CB.snoozeTonight:
      return snooze(deps, id, input, "tonight", actor);
    case CB.snoozeDigest:
      return snooze(deps, id, input, "digest", actor);
    case CB.alwaysMenu:
      return swapKeyboard(deps, id, input, alwaysKeyboard(id), "grant autonomy?");
    case CB.alwaysAll:
      return grantAlways(deps, id, false, input, actor);
    case CB.alwaysRepo:
      return grantAlways(deps, id, true, input, actor);
    case CB.alwaysKeep:
      return swapKeyboard(deps, id, input, escalationKeyboard(id, approveDenyAllowed(deps, id)), "keeping ask");
    case CB.digestUp:
      return digestFeedback(deps, id, "confirmed", actor);
    case CB.digestDown:
      return digestFeedback(deps, id, "overridden", actor);
    case CB.promoteYes:
      return promote(deps, id, false, actor);
    case CB.promoteRepo:
      return promote(deps, id, true, actor);
    case CB.promoteKeep:
      deps.store.audit(actor, "memory.promotion_declined", "memory", id, {});
      return { text: "Keeping ask-first." };
    case CB.probe:
      return probeStuck(deps, id, actor);
    case CB.escalateNow:
      return escalateStuck(deps, id, actor);
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
  actor: string,
): Promise<CallbackAck> {
  const { store } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  if (esc.state === "answered") return { text: "Already answered." };
  if (esc.state !== "pending") return { text: "That card is stale — the escalation is already handled.", alert: true };

  const incident = getIncident(store, esc.incident_id);
  const now = store.clock.now().toISOString();
  const answer = { approval, by: actor };

  const answered = store.db
    .query(
      "UPDATE escalations SET state = 'answered', answer_json = ?, answered_by = ?, answered_at = ? WHERE id = ? AND state = 'pending' AND telegram_message_id = ?",
    )
    .run(JSON.stringify(answer), actor, now, escalationId, input.messageId ?? "");
  if (answered.changes === 0) {
    store.audit("telegram", "telegram.stale_card_rejected", "escalation", escalationId, {
      reason: "lifecycle_cas_failed",
      message_id: input.messageId ?? null,
    });
    return { text: "That card is stale — the escalation changed before this tap was applied.", alert: true };
  }
  if (incident) {
    store.db
      .query("UPDATE incidents SET state = 'resolved', closed_at = ?, snooze_until = NULL WHERE id = ?")
      .run(now, incident.id);
  }
  store.audit(actor, "escalation.answered", "escalation", escalationId, answer);

  // The tap IS the learning signal.
  recordTapOutcome(deps, esc, { approval }, actor);
  recordHumanTap(deps, input, "feedback", "escalation", esc.id, { approval });

  // Route the answer back to the agent.
  let delivery = "skipped";
  if (incident?.car_session_id) {
    delivery = await deps.actions.deliver(
      incident.car_session_id,
      incidentResponseChannel(store, incident),
      { approval },
    );
    store.audit(actor, "escalation.delivered", "escalation", escalationId, { delivery });
  }

  const verb = approval ? "✅ approved" : "❌ denied";
  editEscalationCard(deps, esc, incident, renderResolution(cardText(deps, esc, input), verb, actor).text, []);

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
  actor: string,
): void {
  const decision = latestDecision(deps.store, esc.incident_id);
  if (!decision) {
    deps.store.audit(actor, "outcome.skipped_no_decision", "escalation", esc.id, {});
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
  actor: string,
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
  const snoozed = store.db
    .query("UPDATE escalations SET state = 'snoozed' WHERE id = ? AND state = 'pending' AND telegram_message_id = ?")
    .run(escalationId, input.messageId ?? "");
  if (snoozed.changes === 0) {
    store.audit("telegram", "telegram.stale_card_rejected", "escalation", escalationId, {
      reason: "lifecycle_cas_failed",
      message_id: input.messageId ?? null,
    });
    return { text: "That card is stale — the escalation changed before this tap was applied.", alert: true };
  }
  if (incident) {
    store.db
      .query("UPDATE incidents SET state = 'snoozed', snooze_until = ? WHERE id = ?")
      .run(until.toISOString(), incident.id);
  }
  const label = mode === "1h" ? "in 1h" : mode === "tonight" ? "tonight" : "the next digest";
  store.audit(actor, "incident.snoozed", "incident", incident?.id ?? esc.incident_id, {
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
 * A Telegram Always tap records a learning suggestion and creates a reusable
 * core grant for the exact blocked effect. Memory autonomy is deliberately not
 * an authorization path: providers may learn from the tap, but only the core
 * SafetyKernel can create authority.
 */
function grantAlways(
  deps: HandlerDeps,
  escalationId: string,
  repoOnly: boolean,
  input: CallbackInput,
  actor: string,
): CallbackAck {
  const { store } = deps;
  const esc = getEscalation(store, escalationId);
  if (!esc) return { text: "That escalation is gone.", alert: true };
  const incident = getIncident(store, esc.incident_id);
  const session = incident?.car_session_id ? getSession(store, incident.car_session_id) : null;
  const eventType = incident ? openingEventType(store, incident) : null;

  // An Always tap is meaningful only when the card names the canonical effect
  // that was blocked. Never reconstruct authority from the escalation prose or
  // the legacy suggested approval boolean.
  const suggested = parseSuggestedAction(esc.suggested_action_json);
  const effectIntentId = typeof suggested?.effect_intent_id === "string" ? suggested.effect_intent_id : null;
  if (!effectIntentId) {
    return { text: "Cannot grant: this escalation has no canonical blocked effect.", alert: true };
  }
  const effect = store.getEffectByIntent(effectIntentId);
  if (!effect || effect.state !== "blocked") {
    return { text: "Cannot grant: the canonical effect is missing or no longer blocked.", alert: true };
  }
  if (!session?.vendor || !eventType) {
    return { text: "Cannot grant: the escalation has no verified vendor/event scope.", alert: true };
  }
  const canonicalScope = parseJsonObject(effect.scope_json ?? null);
  if (canonicalScope.vendor !== session.vendor || canonicalScope.event_type !== eventType) {
    return { text: "Cannot grant: the blocked effect scope does not match this escalation.", alert: true };
  }
  const scope: VerifiedScope = { vendor: session.vendor, event_type: eventType };
  if (repoOnly) {
    // A cwd is a mutable execution location, not a verified repository
    // identity. Repo-only grants must fail closed when the session did not
    // carry an authenticated repository label.
    if (session.repo_verified !== 1 || !session.repo) {
      return { text: "Cannot grant for this repo: no verified repository identity is available.", alert: true };
    }
    if (canonicalScope.repo !== session.repo || canonicalScope.repo_verified !== true) {
      return { text: "Cannot grant for this repo: the blocked effect has no matching verified repository scope.", alert: true };
    }
    scope.repo = session.repo;
    scope.repo_verified = true;
  }

  const args = parseJsonObject(effect.args_json);
  const actionClass = effect.action_class;
  if (!actionClass) {
    return { text: "Cannot grant: the blocked effect has no canonical action class.", alert: true };
  }
  const grantIntentId = `telegram:grant:${escalationId}:${repoOnly ? "repo" : "global"}`;
  let grant = existingGrant(deps, grantIntentId);
  if (!grant) {
    try {
      grant = deps.safety.createGrant({
        intent_id: grantIntentId,
        lineage: null,
        scope,
        effect_type: effect.type as EffectType,
        constraints: { args, action_class: actionClass },
        uses_remaining: null,
        created_by: "human",
        provenance: {
          source: "telegram_always_button",
          escalation_id: escalationId,
          effect_intent_id: effectIntentId,
          repo_only: repoOnly,
        },
      });
    } catch (error) {
      store.audit(actor, "grant.creation_failed", "escalation", escalationId, { error: String(error) });
      return { text: "Cannot grant: the core safety kernel rejected this authority.", alert: true };
    }
  }

  const content: Record<string, unknown> = {
    match: incident?.dedupe_class ?? eventType ?? esc.question.slice(0, 120),
    disposition: "suggest",
    action_class: actionClass,
    args_template: args,
    origin: "telegram_always_button",
    question: esc.question.slice(0, 200),
    effect_intent_id: effectIntentId,
    grant_id: grant.id,
  };
  const memoryScope = scope as Record<string, unknown>;
  const existing = findRule(store, String(content.match), memoryScope);
  const memoryId = existing ?? deps.memoryWriter.addFromDavid("rule", "autonomy", content, memoryScope);
  store.audit(actor, "memory.suggestion_from_escalation", "memory", memoryId, {
    escalation_id: escalationId,
    effect_intent_id: effectIntentId,
    grant_id: grant.id,
    repo_only: repoOnly,
    reused: Boolean(existing),
  });
  recordHumanTap(deps, input, "grant_created", "grant", grant.id, {
    escalation_id: escalationId,
    effect_intent_id: effectIntentId,
    repo_only: repoOnly,
    scope,
  });
  store.audit(actor, "telegram.grant_created", "grant", grant.id, {
    escalation_id: escalationId,
    effect_intent_id: effectIntentId,
    repo_only: repoOnly,
  });

  editEscalationCard(
    deps,
    esc,
    incident,
    `${cardText(deps, esc, input)}\n🧠 Always${repoOnly ? " (this repo)" : ""}: core grant created.`,
    escalationKeyboard(escalationId, approveDenyAllowed(deps, escalationId)),
  );
  return { text: `🧠 Granted${repoOnly ? " for this repo" : ""}. Still needs your call on this one.` };
}

function parseSuggestedAction(value: string | null): Record<string, unknown> | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function parseJsonObject(value: string | null): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function existingGrant(deps: HandlerDeps, intentId: string) {
  const row = deps.store.db.query("SELECT id FROM grants WHERE intent_id = ?").get(intentId) as { id: string } | null;
  return row ? deps.safety.getGrant(row.id) : null;
}

function recordHumanTap(
  deps: HandlerDeps,
  input: CallbackInput,
  kind: "grant_created" | "feedback" | "instruction" | "reply",
  targetType: string,
  targetId: string,
  body: Record<string, unknown>,
): void {
  try {
    deps.store.recordHumanFact({
      sourceId: "telegram",
      idempotencyKey: `callback:${input.messageId ?? "unknown"}:${input.data}`,
      kind,
      targetType,
      targetId,
      actorId: input.from!,
      body: { ...body, from: input.from },
    });
  } catch (error) {
    // The grant has already been durably recorded. Keep the tap visible in
    // audit if a replay races the interaction reservation.
    deps.store.audit("daemon", "human_fact.record_failed", targetType, targetId, { error: String(error) });
  }
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

function digestFeedback(
  deps: HandlerDeps,
  decisionId: string,
  verdict: "confirmed" | "overridden",
  actor: string,
): CallbackAck {
  const row = deps.store.db.query("SELECT id FROM decisions WHERE id = ?").get(decisionId) as { id: string } | null;
  if (!row) return { text: "That decision is gone." };
  const interaction = deps.store.recordHumanFact({
    sourceId: "telegram",
    idempotencyKey: `digest-feedback:${decisionId}:${verdict}:${actor}`,
    kind: "feedback",
    targetType: "decision",
    targetId: decisionId,
    actorId: actor,
    body: { verdict, via: "digest" },
  });
  if (!interaction.inserted) {
    return { text: verdict === "confirmed" ? "👍 already noted" : "👎 already noted" };
  }
  deps.memoryWriter.recordOutcome({ decisionId, verdict, davidAction: { via: "digest" } });
  deps.store.audit(actor, "digest.feedback", "decision", decisionId, { verdict });
  return { text: verdict === "confirmed" ? "👍 noted" : "👎 noted — that rule is demoted." };
}

/**
 * Digest promotion offer: "Auto-approve these? [Yes] [Yes, this repo only] [Keep asking]".
 * A promotion is still a learning signal; it does not grant runtime authority
 * without a canonical blocked effect for the core SafetyKernel to review.
 */
function promote(deps: HandlerDeps, memoryId: string, repoOnly: boolean, actor: string): CallbackAck {
  const { store } = deps;
  const row = store.db
    .query("SELECT id, tier, kind, content_json, scope_json FROM memories WHERE id = ?")
    .get(memoryId) as
    | { id: string; tier: string; kind: string; content_json: string; scope_json: string }
    | null;
  if (!row) return { text: "That proposal is gone." };

  if (!repoOnly) {
    store.audit(actor, "memory.promotion_recorded", "memory", memoryId, { repo_only: false });
    return { text: "🧠 Recorded as a suggestion. A specific effect still needs a core grant." };
  }

  // Narrower grant: a repo-scoped copy is granted, the broad rule stays at suggest.
  const scope = safeParse(row.scope_json);
  const repo = inferRepoForMemory(store, memoryId) ?? (scope.repo as string | undefined);
  if (!repo) {
    store.audit(actor, "memory.promotion_rejected", "memory", memoryId, {
      repo_only: true,
      reason: "no verified repository identity",
    });
    return { text: "Cannot record this repo-only suggestion: no verified repository identity is available.", alert: true };
  }
  const narrowed = deps.memoryWriter.addFromDavid(
    row.tier,
    row.kind,
    { ...safeParse(row.content_json), narrowed_from: memoryId },
    { ...scope, repo },
  );
  store.audit(actor, "memory.promotion_recorded_repo_scoped", "memory", narrowed, { from: memoryId, repo });
  return { text: `🧠 Recorded as a suggestion for ${repo} only. A specific effect still needs a core grant.` };
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
function probeStuck(deps: HandlerDeps, carSessionId: string, actor: string): CallbackAck {
  const { store, config } = deps;
  const session = getSession(store, carSessionId);
  if (!session) return { text: "Session not found." };

  const probe = session.cwd
    ? { templateId: "git.status", args: { repo: session.cwd } as Record<string, unknown> }
    : { templateId: "agentctl.recent", args: { limit: 10 } as Record<string, unknown> };
  store.audit(actor, "digest.probe_requested", "session", carSessionId, probe);

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

function escalateStuck(deps: HandlerDeps, carSessionId: string, actor: string): CallbackAck {
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
  store.audit(actor, "digest.escalate_requested", "session", carSessionId, {});
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
  const actor = telegramActor(deps.config, msg.from);
  if (!actor) {
    rejectTelegramActor(store, msg.from, "message", msg.messageId);
    return { kind: "ignored", reason: "unauthorized" };
  }
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
    const answered = store.db
      .query(
        "UPDATE escalations SET state = 'answered', answer_json = ?, answered_by = ?, answered_at = ? WHERE id = ? AND state = 'pending' AND telegram_message_id = ?",
      )
      .run(JSON.stringify({ text }), actor, now, esc.id, msg.replyToMessageId ?? "");
    if (answered.changes === 0) {
      store.audit("telegram", "telegram.stale_card_rejected", "escalation", esc.id, {
        reason: "lifecycle_cas_failed",
        message_id: msg.replyToMessageId ?? null,
      });
      return { kind: "ignored", reason: "stale_escalation" };
    }
    if (incident) {
      store.db.query("UPDATE incidents SET state = 'resolved', closed_at = ? WHERE id = ?").run(now, incident.id);
    }
    // Free text instead of a tap: recorded as a correction, never as a confirm.
    recordTapOutcome(deps, esc, { text }, actor);
    store.audit(actor, "escalation.answered_text", "escalation", esc.id, { chars: text.length });
  }

  const channel = incident ? incidentResponseChannel(store, incident) : null;
  recordHumanTap(
    deps,
    { data: `message:${msg.messageId}`, messageId: msg.messageId, ...(msg.from ? { from: msg.from } : {}) },
    "reply",
    "session",
    carSessionId,
    { text, escalation_id: esc?.id ?? null },
  );
  const result = await deps.actions.deliver(carSessionId, channel, { text });
  store.audit(actor, "reply.delivered", "session", carSessionId, {
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
    payload: { from: msg.from!, telegram_message_id: msg.messageId },
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
  const actor = telegramActor(config, input.from);
  if (!actor) {
    rejectTelegramActor(store, input.from, "command", cmd);
    return "not authorized";
  }
  store.audit(actor, "telegram.command", "command", cmd, { args: input.args.slice(0, 200) });

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
      return muteSession(deps, input.args, actor);
    case "panic":
      return await panic(deps, input.args, actor);
    case "ticker": {
      const arg = input.args.trim().toLowerCase();
      if (arg !== "on" && arg !== "off") return "Usage: /ticker on|off";
      store.kvSet(KV_TICKER, arg === "on");
      store.audit(actor, "telegram.ticker", "kv", KV_TICKER, { enabled: arg === "on" });
      return `Ticker ${arg}.`;
    }
    case "policy": {
      const snapshot = deps.safety.snapshot();
      return [
        "⚖️ Core safety",
        `mode: ${snapshot.panic || snapshot.breaker_open ? "ESCALATE-ONLY" : "normal"}`,
        `panic: ${snapshot.panic ? snapshot.panic_reason ?? "active" : "off"}`,
        `breaker: ${snapshot.breaker_open ? "open" : "closed"}`,
        `attempts: ${snapshot.attempts} · failures: ${snapshot.failures} · effect spend: $${snapshot.spent_usd.toFixed(2)}`,
        "Provider policy is advisory; human grants and these rails are core-owned.",
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
  const outbox = one<{ pending: number; failed: number; uncertain: number; deferred: number }>(
    `SELECT SUM(state = 'pending') AS pending,
            SUM(state IN ('failed','dead')) AS failed,
            SUM(state = 'uncertain') AS uncertain,
            SUM(state = 'deferred') AS deferred
     FROM outbox`,
  );
  const spend = store.spendToday();
  const escalateOnly = deps.safety.snapshot().panic;

  return [
    "📊 CAR status",
    `🙋 escalations pending: ${escalations.pending ?? 0}`,
    `📁 incidents: ${incidents.open ?? 0} open · ${incidents.escalated ?? 0} escalated · ${incidents.snoozed ?? 0} snoozed`,
    `🧑‍💻 sessions: ${sessions.active ?? 0} active · ${sessions.muted ?? 0} muted`,
    `📮 outbox: ${outbox.pending ?? 0} pending · ${outbox.deferred ?? 0} held · ${outbox.uncertain ?? 0} uncertain · ${outbox.failed ?? 0} failed`,
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

function muteSession(deps: HandlerDeps, args: string, actor: string): string {
  const { store } = deps;
  const [ref, dur] = args.trim().split(/\s+/);
  if (!ref || !dur) return "Usage: /mute <session> <2h|30m|1d>";
  const ms = parseDuration(dur);
  if (ms === null) return `Cannot parse duration "${dur}". Use 30m, 2h, 1d.`;
  const carSessionId = resolveSessionRef(store, ref);
  if (!carSessionId) return `No session matching "${ref}".`;
  const until = new Date(store.clock.now().getTime() + ms).toISOString();
  store.db.query("UPDATE sessions SET muted_until = ? WHERE car_session_id = ?").run(until, carSessionId);
  store.audit(actor, "session.muted", "session", carSessionId, { until });
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

/** /panic — durable core panic and cancellation of compatibility actions. */
async function panic(deps: HandlerDeps, args: string, actor: string): Promise<string> {
  const { store } = deps;
  const off = args.trim().toLowerCase() === "off";

  if (off) {
    deps.safety.clearPanic();
    const snapshot = deps.safety.snapshot();
    store.audit(actor, "panic.cleared", "safety", "global", { snapshot });
    return `✅ Escalate-only cleared. CAR may act autonomously again. (panic=${snapshot.panic})`;
  }

  deps.safety.panic("telegram /panic");
  store.db
    .query("UPDATE actions SET state = 'failed', result_json = ? WHERE state IN ('pending','running')")
    .run(JSON.stringify({ cancelled_by: "panic" }));
  const snapshot = deps.safety.snapshot();
  store.audit(actor, "panic.engaged", "safety", "global", { snapshot });
  return `🛑 ESCALATE-ONLY. In-flight actions cancelled; CAR will ask before anything. (panic=${snapshot.panic}) /panic off to clear.`;
}

/** Compatibility read for surfaces that still ask for the old mode label. */
export function escalateOnlyMode(store: Store): boolean {
  return store.getPanicState().active;
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
