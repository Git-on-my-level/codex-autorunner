import { randomUUID } from "node:crypto";
import type { CarConfig } from "../config/config.ts";
import type { Loop } from "../ports.ts";
import type { Store } from "../store/db.ts";

export const DEADMAN_CONTRACT = "car.deadman.v1";
const STATUS_KEY = "deadman:last_status";
const SUCCESS_KEY = "deadman:last_success";

export interface DeadmanPayload {
  contract: typeof DEADMAN_CONTRACT;
  daemon_instance_id: string;
  seq: number;
  ts: string;
  last_durable_progress_at: string | null;
  last_digest_receipt_at: string | null;
}

export interface DeadmanOptions {
  fetch?: typeof globalThis.fetch;
  instanceId?: string;
}

export function createDeadmanLoop(store: Store, config: CarConfig, opts: DeadmanOptions = {}): Loop {
  const send = opts.fetch ?? globalThis.fetch;
  const instanceId = opts.instanceId ?? randomUUID();
  let seq = 0;
  let timer: ReturnType<typeof setInterval> | null = null;
  let running = false;

  async function heartbeat(): Promise<void> {
    if (!config.deadman.enabled || running) return;
    running = true;
    const now = store.clock.now().toISOString();
    const payload = buildPayload(store, instanceId, ++seq, now);
    try {
      const token = process.env[config.deadman.token_env];
      if (!token) throw new Error(`missing $${config.deadman.token_env}`);
      const response = await send(config.deadman.url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(config.deadman.timeout_seconds * 1000),
      });
      if (!response.ok) throw new Error(`observer returned ${response.status}`);
      transition(store, "healthy", { seq, observer: observerLabel(config.deadman.url) });
      store.kvSet(SUCCESS_KEY, { at: now, seq, daemon_instance_id: instanceId });
    } catch (error) {
      transition(store, "failed", {
        seq,
        observer: observerLabel(config.deadman.url),
        error: String(error),
      });
    } finally {
      running = false;
    }
  }

  return {
    name: "deadman-heartbeat",
    async start() {
      if (!config.deadman.enabled) return;
      await heartbeat();
      timer = setInterval(() => void heartbeat(), config.deadman.interval_seconds * 1000);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}

export function buildPayload(store: Store, instanceId: string, seq: number, now: string): DeadmanPayload {
  // Replay the receipt-to-digest projection before sampling health evidence so
  // a crash between those durable writes cannot hide a delivered digest.
  store.reconcileDigestReceipts();
  const progress = store.db
    .query("SELECT MAX(ts) AS ts FROM audit WHERE verb NOT LIKE 'deadman.%'")
    .get() as { ts: string | null };
  // `sent_at` is a receipt projection, not an enqueue timestamp. Restrict the
  // evidence to canonical delivered outbox rows so deadman never reports a
  // digest that merely made it into the local queue.
  const digest = store.db
    .query(
      `SELECT MAX(d.sent_at) AS ts
       FROM digests d JOIN outbox o ON o.id = d.outbox_id
       WHERE d.sent_at IS NOT NULL AND o.state = 'delivered'`,
    )
    .get() as { ts: string | null };
  return {
    contract: DEADMAN_CONTRACT,
    daemon_instance_id: instanceId,
    seq,
    ts: now,
    last_durable_progress_at: progress.ts,
    last_digest_receipt_at: digest.ts,
  };
}

function transition(store: Store, status: "healthy" | "failed", detail: Record<string, unknown>): void {
  const previous = store.kvGet<string>(STATUS_KEY);
  store.kvSet(STATUS_KEY, status);
  if (previous === status) return;
  store.audit("daemon", `deadman.${status}`, "observer", "external", { previous, ...detail });
}

function observerLabel(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "invalid-url";
  }
}
