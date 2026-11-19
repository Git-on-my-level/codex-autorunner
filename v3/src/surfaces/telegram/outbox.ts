/**
 * Outbox deliverer — the only code path that talks to Telegram.
 *
 * Deliberately a pure function over `(store, sendFn)` so it is tested with a
 * fake send and a fake clock; grammY only ever supplies the sendFn. Crash-only:
 * every attempt is a row update, nothing authoritative lives in memory.
 */
import type { Store } from "../../store/db.ts";
import { createHash } from "node:crypto";
import { OUTBOX_STATE, TELEGRAM_CHANNEL, type MessageSpec, type OutboxRow, type TelegramSendFn, type TelegramTarget } from "./types.ts";

export interface DelivererOptions {
  batchSize?: number;
  /** Attempts before a row is declared dead (and loudly audited). */
  maxAttempts?: number;
  baseBackoffSeconds?: number;
  maxBackoffSeconds?: number;
  /** Fault-injection seam used to prove receipt-first crash recovery. */
  afterReceipt?: (row: OutboxRow) => void;
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
export function enqueueMessage(
  store: Store,
  target: TelegramTarget,
  spec: MessageSpec,
  opts: { intentId?: string; route?: unknown } = {},
): number {
  return store.enqueueOutboxIntent({
    channel: TELEGRAM_CHANNEL,
    target,
    body: spec,
    ...(opts.intentId ? { intentId: opts.intentId } : {}),
    ...(opts.route !== undefined ? { route: opts.route } : {}),
  }).outboxId;
}

/** Stable identity for the scheduled digest for a local calendar day. */
export function digestIntentId(day: string): string {
  return `telegram-digest:${createHash("sha256").update(day).digest("hex").slice(0, 32)}`;
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
  const owner = "telegram-outbox";
  const rows = store.claimPendingOutbox(cfg.batchSize, owner, 120) as OutboxRow[];
  const stats: DelivererStats = { sent: 0, retried: 0, dead: 0, deferred: 0 };

  // A previous process may have committed the canonical receipt and died
  // before creating the callback/edit projections. Rebuild those before doing
  // any new remote work; no transport call is involved in reconciliation.
  reconcileTelegramDeliveryProjections(store);

  for (const row of rows) {
    const claim = { owner, token: row.claim_token ?? "" };
    if (!claim.token) {
      store.audit("daemon", "outbox.claim_missing", "outbox", String(row.id), {});
      continue;
    }
    let target: TelegramTarget;
    let spec: MessageSpec;
    try {
      target = JSON.parse(row.target_json) as TelegramTarget;
      spec = JSON.parse(row.body_json) as MessageSpec;
    } catch (err) {
      markDead(store, row, claim, `unparseable row: ${String(err)}`);
      stats.dead++;
      continue;
    }

    // A replayed or delayed notification must not resurrect answered, cancelled
    // or snoozed attention. Edits still flow so old cards can show their outcome.
    if (target.kind === "escalation" && target.escalation_id) {
      const esc = store.db.query("SELECT state FROM escalations WHERE id=?").get(target.escalation_id) as { state: string } | null;
      if (esc && esc.state !== "pending") {
        store.recordOutboxReceipt(row.id, claim, "superseded", { error: { reason: "escalation_not_pending", state: esc.state } });
        store.audit("daemon", "outbox.stale_card_suppressed", "outbox", String(row.id), { escalation_id: target.escalation_id });
        continue;
      }
    }

    // Quiet hours / policy decided this must not push. Park it for the digest.
    if (target.queue_for_digest) {
      if (!store.deferOutbox(row.id, claim)) {
        store.audit("daemon", "outbox.defer_claim_lost", "outbox", String(row.id), {});
        continue;
      }
      store.audit("daemon", "outbox.deferred_to_digest", "outbox", String(row.id), {
        kind: target.kind,
      });
      stats.deferred++;
      continue;
    }

    let result;
    try {
      result = await send(target, spec);
    } catch (err) {
      const attempts = row.attempts + 1;
      if (attempts >= cfg.maxAttempts) {
        markDead(store, row, claim, String(err), attempts);
        stats.dead++;
      } else {
        const nextAt = new Date(
          now.getTime() + backoffSeconds(attempts, cfg.baseBackoffSeconds, cfg.maxBackoffSeconds) * 1000,
        ).toISOString();
        if (store.rescheduleOutbox(row.id, claim, nextAt, err)) stats.retried++;
        else store.audit("daemon", "outbox.retry_claim_lost", "outbox", String(row.id), { error: String(err) });
      }
      continue;
    }

    const messageId = result.message_id ?? target.edit_message_id ?? null;
    try {
      // This is the canonical remote receipt. If this write loses its lease or
      // the process crashes before it, startup recovery leaves `uncertain` and
      // never turns the row into an ordinary automatic retry. Transport data
      // required by projections is part of the same durable receipt.
      store.recordOutboxReceipt(row.id, claim, "delivered", {
        sentMessageId: messageId,
        transportResult: { thread_id: result.thread_id ?? null },
      });
    } catch (err) {
      store.audit("daemon", "outbox.receipt_failed", "outbox", String(row.id), {
        error: String(err),
        remote_delivery: "possibly_delivered",
      });
      continue;
    }

    stats.sent++;
    store.audit("daemon", "outbox.sent", "outbox", String(row.id), {
      kind: target.kind,
      message_id: messageId,
    });

    try {
      cfg.afterReceipt?.(row);
      // Both are terminal-receipt projections and are independently replayed.
      store.reconcileDigestReceipts();
      reconcileTelegramDeliveryProjections(store);
    } catch (err) {
      store.audit("daemon", "outbox.projection_failed", "outbox", String(row.id), {
        error: String(err),
        remote_delivery: "delivered",
      });
    }
  }
  return stats;
}

function markDead(store: Store, row: OutboxRow, claim: { owner: string; token: string }, error: string, attempts = row.attempts + 1): void {
  store.recordOutboxReceipt(row.id, claim, "failed", { error: { attempts, error } });
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
  receiptAt: string,
  outboxId: number,
): void {
  if (threadId && target.car_session_id) {
    store.db
      .query("UPDATE sessions SET telegram_thread_id = ? WHERE car_session_id = ? AND telegram_thread_id IS NULL")
      .run(threadId, target.car_session_id);
  }
  if (!messageId) return;

  if (target.kind === "escalation" && target.escalation_id) {
    const key = `tg.latest_escalation_receipt.${target.escalation_id}`;
    if ((store.kvGet<number>(key) ?? -1) < outboxId) {
      store.db.query("UPDATE escalations SET telegram_message_id = ?, sent_at = ? WHERE id = ?")
        .run(messageId, receiptAt, target.escalation_id);
      store.kvSet(key, outboxId);
    }
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

interface DeliveredProjectionRow {
  id: number;
  target_json: string;
  sent_message_id: string | null;
  result_json: string | null;
}

const projectionKey = (outboxId: number): string => `tg.delivery_projection.${outboxId}`;

function parseReceipt(resultJson: string | null): { receiptAt: string; threadId: string | null } {
  if (!resultJson) throw new Error("delivered Telegram outbox row has no receipt");
  const parsed = JSON.parse(resultJson) as {
    receipt_at?: unknown;
    transport_result?: { thread_id?: unknown } | null;
  };
  if (typeof parsed.receipt_at !== "string") throw new Error("delivered Telegram outbox row has no receipt_at");
  const threadId = parsed.transport_result?.thread_id;
  return {
    receiptAt: parsed.receipt_at,
    threadId: typeof threadId === "string" ? threadId : null,
  };
}

/**
 * Rebuild Telegram callback/edit projections solely from canonical delivered
 * outbox rows. Each row is one transaction: either every projection and its
 * marker commit, or a later pass retries the whole deterministic operation.
 * This function never sends a Telegram message.
 */
export function reconcileTelegramDeliveryProjections(store: Store): number {
  const rows = store.db
    .query(
      `SELECT o.id, o.target_json, o.sent_message_id, o.result_json
       FROM outbox o
       LEFT JOIN kv marker ON marker.key = 'tg.delivery_projection.' || CAST(o.id AS TEXT)
       WHERE o.channel = ? AND o.state = 'delivered' AND marker.key IS NULL
       ORDER BY o.id ASC`,
    )
    .all(TELEGRAM_CHANNEL) as DeliveredProjectionRow[];
  let reconciled = 0;

  for (const row of rows) {
    try {
      const target = JSON.parse(row.target_json) as TelegramTarget;
      const receipt = parseReceipt(row.result_json);
      const applied = store.db.transaction(() => {
        if (store.kvGet(projectionKey(row.id)) !== null) return false;
        applySendSideEffects(store, target, row.sent_message_id, receipt.threadId, receipt.receiptAt, row.id);
        store.kvSet(projectionKey(row.id), {
          state: "applied",
          receipt_at: receipt.receiptAt,
        });
        store.audit("daemon", "telegram.delivery_projected", "outbox", String(row.id), {
          message_id: row.sent_message_id,
          kind: target.kind,
        });
        return true;
      })();
      if (applied) reconciled++;
    } catch (err) {
      // Leave the marker absent so a repaired row/code path can be replayed.
      store.audit("daemon", "telegram.delivery_projection_failed", "outbox", String(row.id), {
        error: String(err),
      });
    }
  }
  return reconciled;
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
