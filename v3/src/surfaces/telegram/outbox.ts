/**
 * Outbox deliverer — the only code path that talks to Telegram.
 *
 * Deliberately a pure function over `(store, sendFn)` so it is tested with a
 * fake send and a fake clock; grammY only ever supplies the sendFn. Crash-only:
 * every attempt is a row update, nothing authoritative lives in memory.
 */
import type { Store } from "../../store/db.ts";
import { OUTBOX_STATE, TELEGRAM_CHANNEL, type MessageSpec, type OutboxRow, type TelegramSendFn, type TelegramTarget } from "./types.ts";

export interface DelivererOptions {
  batchSize?: number;
  /** Attempts before a row is declared dead (and loudly audited). */
  maxAttempts?: number;
  baseBackoffSeconds?: number;
  maxBackoffSeconds?: number;
}

export interface DelivererStats {
  sent: number;
  retried: number;
  dead: number;
  deferred: number;
}

const DEFAULTS = {
  batchSize: 20,
  maxAttempts: 6,
  baseBackoffSeconds: 5,
  maxBackoffSeconds: 900,
};

/** Exponential backoff: 5s, 10s, 20s, 40s, 80s… capped. `attempts` is post-increment. */
export function backoffSeconds(attempts: number, base = DEFAULTS.baseBackoffSeconds, cap = DEFAULTS.maxBackoffSeconds): number {
  const raw = base * Math.pow(2, Math.max(0, attempts - 1));
  return Math.min(raw, cap);
}

/** The single enqueue point for this surface: everything outbound is a row first. */
export function enqueueMessage(store: Store, target: TelegramTarget, spec: MessageSpec): number {
  return store.enqueueOutbox(TELEGRAM_CHANNEL, target, spec);
}

export function pendingOutbox(store: Store, limit: number, now: string): OutboxRow[] {
  return store.db
    .query(
      `SELECT * FROM outbox
       WHERE channel = ? AND state = ? AND next_attempt_at <= ?
       ORDER BY id ASC LIMIT ?`,
    )
    .all(TELEGRAM_CHANNEL, OUTBOX_STATE.pending, now, limit) as OutboxRow[];
}

/**
 * Drain one batch. Returns per-row outcome counts. Never throws for a send
 * failure — a failing row is rescheduled or declared dead, and the loop
 * continues with the rest of the batch.
 */
export async function deliverOutboxOnce(
  store: Store,
  send: TelegramSendFn,
  opts: DelivererOptions = {},
): Promise<DelivererStats> {
  const cfg = { ...DEFAULTS, ...opts };
  const now = store.clock.now();
  const rows = pendingOutbox(store, cfg.batchSize, now.toISOString());
  const stats: DelivererStats = { sent: 0, retried: 0, dead: 0, deferred: 0 };

  for (const row of rows) {
    let target: TelegramTarget;
    let spec: MessageSpec;
    try {
      target = JSON.parse(row.target_json) as TelegramTarget;
      spec = JSON.parse(row.body_json) as MessageSpec;
    } catch (err) {
      markDead(store, row, `unparseable row: ${String(err)}`);
      stats.dead++;
      continue;
    }

    // Quiet hours / policy decided this must not push. Park it for the digest.
    if (target.queue_for_digest) {
      store.db
        .query("UPDATE outbox SET state = ? WHERE id = ?")
        .run(OUTBOX_STATE.deferred, row.id);
      store.audit("daemon", "outbox.deferred_to_digest", "outbox", String(row.id), {
        kind: target.kind,
      });
      stats.deferred++;
      continue;
    }

    try {
      const result = await send(target, spec);
      const messageId = result.message_id ?? target.edit_message_id ?? null;
      store.db
        .query("UPDATE outbox SET state = ?, attempts = attempts + 1, sent_message_id = ? WHERE id = ?")
        .run(OUTBOX_STATE.sent, messageId, row.id);
      applySendSideEffects(store, target, messageId, result.thread_id ?? null);
      store.audit("daemon", "outbox.sent", "outbox", String(row.id), {
        kind: target.kind,
        message_id: messageId,
      });
      stats.sent++;
    } catch (err) {
      const attempts = row.attempts + 1;
      if (attempts >= cfg.maxAttempts) {
        markDead(store, row, String(err), attempts);
        stats.dead++;
      } else {
        const nextAt = new Date(
          now.getTime() + backoffSeconds(attempts, cfg.baseBackoffSeconds, cfg.maxBackoffSeconds) * 1000,
        ).toISOString();
        store.db
          .query("UPDATE outbox SET attempts = ?, next_attempt_at = ?, state = ? WHERE id = ?")
          .run(attempts, nextAt, OUTBOX_STATE.pending, row.id);
        store.audit("daemon", "outbox.retry", "outbox", String(row.id), {
          attempts,
          next_attempt_at: nextAt,
          error: String(err),
        });
        stats.retried++;
      }
    }
  }
  return stats;
}

function markDead(store: Store, row: OutboxRow, error: string, attempts = row.attempts + 1): void {
  store.db
    .query("UPDATE outbox SET state = ?, attempts = ? WHERE id = ?")
    .run(OUTBOX_STATE.dead, attempts, row.id);
  // Non-negotiable #6: a message that cannot reach David is loud, never silent.
  store.audit("daemon", "outbox.dead", "outbox", String(row.id), { attempts, error });
}

/**
 * Record what the delivered message id unlocks: edit-in-place targets and the
 * reply-routing map (telegram message → escalation → incident → session).
 */
function applySendSideEffects(
  store: Store,
  target: TelegramTarget,
  messageId: string | null,
  threadId: string | null,
): void {
  if (threadId && target.car_session_id) {
    store.db
      .query("UPDATE sessions SET telegram_thread_id = ? WHERE car_session_id = ? AND telegram_thread_id IS NULL")
      .run(threadId, target.car_session_id);
  }
  if (!messageId) return;

  if (target.kind === "escalation" && target.escalation_id) {
    store.db
      .query("UPDATE escalations SET telegram_message_id = ?, sent_at = ? WHERE id = ?")
      .run(messageId, store.clock.now().toISOString(), target.escalation_id);
  }
  if (target.kind === "escalation" && target.incident_id) {
    store.db
      .query("UPDATE incidents SET telegram_message_id = COALESCE(telegram_message_id, ?) WHERE id = ?")
      .run(messageId, target.incident_id);
  }
  if (target.escalation_id || target.car_session_id) {
    // Reply routing map: any inbound reply to this message id lands here.
    store.kvSet(msgMapKey(messageId), {
      escalation_id: target.escalation_id ?? null,
      incident_id: target.incident_id ?? null,
      car_session_id: target.car_session_id ?? null,
    });
  }
  if (target.become_anchor && target.car_session_id) {
    // First delivered message wins: two rows enqueued before either was sent
    // must not leave the session pointing at the later anchor.
    if (!store.kvGet<string>(anchorKey(target.car_session_id))) {
      store.kvSet(anchorKey(target.car_session_id), messageId);
    }
  }
  if (target.kind === "ticker" && target.car_session_id) {
    store.kvSet(tickerKey(target.car_session_id), messageId);
  }
}

export interface MsgMapEntry {
  escalation_id: string | null;
  incident_id: string | null;
  car_session_id: string | null;
}

export const msgMapKey = (messageId: string): string => `tg.msg.${messageId}`;
export const anchorKey = (carSessionId: string): string => `tg.anchor.${carSessionId}`;
export const tickerKey = (carSessionId: string): string => `tg.ticker.${carSessionId}`;

export function lookupMessage(store: Store, messageId: string): MsgMapEntry | null {
  return store.kvGet<MsgMapEntry>(msgMapKey(messageId));
}
