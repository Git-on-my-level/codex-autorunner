/**
 * WS-D — Telegram surface. DESIGN §7.
 *
 * Shape of this module:
 *   render.ts    pure message rendering + callback codec (no store, no grammY)
 *   targets.ts   forum-topic vs flat-anchor routing (pure over the store)
 *   handlers.ts  taps / replies / commands — writes rows, never calls Telegram
 *   outbox.ts    the deliverer: pure over (store, sendFn), exponential backoff
 *   bot.ts       the only grammY import, loaded lazily when a token exists
 *
 * ChannelPort methods never talk to Telegram: they render a message spec and
 * enqueue it. The daemon can therefore run (and be tested) with Telegram off.
 */
import type { Store } from "../../store/db.ts";
import type { CarConfig } from "../../config/config.ts";
import type { ActionBus, ChannelPort, EscalationMessage, Loop, MemoryWriter } from "../../ports.ts";
import { KV_TICKER, updateTicker, type HandlerDeps } from "./handlers.ts";
import { deliverOutboxOnce, enqueueMessage, type DelivererStats } from "./outbox.ts";
import { renderDigestMessage, renderEscalation } from "./render.ts";
import { getIncident, openingEventType, type IncidentRow } from "./rows.ts";
import { getSession, isMuted, resolveTarget, sessionLabel } from "./targets.ts";
import type { MessageSpec, TelegramSendFn, TelegramTarget } from "./types.ts";

export interface TelegramChannel extends ChannelPort {
  loop: Loop;
  /** Drain one outbox batch. Exposed for tests and the e2e smoke. */
  deliverOnce(send?: TelegramSendFn): Promise<DelivererStats>;
  /** One edited-in-place status line per session; no-op unless /ticker is on. */
  updateTicker(carSessionId: string, line: string): boolean;
}

const DELIVER_INTERVAL_MS = 2000;

export function createTelegram(
  store: Store,
  config: CarConfig,
  actions: ActionBus,
  memoryWriter: MemoryWriter,
): TelegramChannel {
  const handlerDeps: HandlerDeps = {
    store,
    config,
    actions,
    memoryWriter,
    host: process.env.CAR_HOST ?? "localhost",
  };

  let sendFn: TelegramSendFn | null = null;
  let botHandle: { stop(): Promise<void> } | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;
  let draining = false;

  /**
   * Quiet hours are enforced upstream; here we only honour an explicit
   * `queue_for_digest` request and mute the sessions David silenced. Urgent
   * always breaks through (DESIGN §7).
   */
  function applyHold(target: TelegramTarget, severity: string | null, requested?: boolean): void {
    if (severity === "urgent") return;
    if (requested === true) {
      target.queue_for_digest = true;
      return;
    }
    if (isMuted(store, target.car_session_id ?? null, store.clock.now())) {
      target.queue_for_digest = true;
    }
  }

  return {
    sendEscalation(msg: EscalationMessage): void {
      const incident: IncidentRow | null = getIncident(store, msg.incidentId);
      const session = msg.carSessionId ? getSession(store, msg.carSessionId) : null;
      const openingType = incident ? openingEventType(store, incident) : null;
      const rendered = renderEscalation({
        ...msg,
        ...(sessionLabel(session) ? { sessionLabel: sessionLabel(session)! } : {}),
        allowApproveDeny: openingType !== "attention.question",
      });

      const target = resolveTarget(store, config, "escalation", msg.carSessionId);
      target.escalation_id = msg.escalationId;
      target.incident_id = msg.incidentId;
      applyHold(target, msg.severity, (msg as { queue_for_digest?: boolean }).queue_for_digest);

      const spec: MessageSpec = { text: rendered.text, inline_keyboard: rendered.inline_keyboard };
      enqueueMessage(store, target, spec);
      store.audit("daemon", "escalation.enqueued", "escalation", msg.escalationId, {
        severity: msg.severity,
        held: target.queue_for_digest === true,
      });
    },

    sendNotify(text: string, carSessionId?: string): void {
      const target = resolveTarget(store, config, "notify", carSessionId ?? null);
      applyHold(target, null);
      enqueueMessage(store, target, { text });
    },

    sendDigest(markdown: string): void {
      // The digest never queues for itself and never respects a mute.
      enqueueMessage(
        store,
        { kind: "digest", chat_id: config.telegram.chat_id },
        renderDigestMessage(markdown),
      );
      store.audit("daemon", "digest.enqueued", "digest", store.clock.now().toISOString().slice(0, 10), {});
    },

    async deliverOnce(send?: TelegramSendFn): Promise<DelivererStats> {
      const fn = send ?? sendFn;
      if (!fn) return { sent: 0, retried: 0, dead: 0, deferred: 0 };
      return deliverOutboxOnce(store, fn);
    },

    updateTicker(carSessionId: string, line: string): boolean {
      return updateTicker(handlerDeps, carSessionId, line);
    },

    loop: {
      name: "telegram",
      async start(): Promise<void> {
        if (!config.telegram.enabled) return;
        const token = process.env[config.telegram.token_env];
        if (!token) {
          store.audit("daemon", "telegram.disabled", "daemon", "card", {
            reason: `${config.telegram.token_env} not set`,
          });
          return;
        }
        // Lazy: grammY is only imported when a real bot is actually wanted.
        const { createBot } = await import("./bot.ts");
        const handle = createBot(token, handlerDeps);
        sendFn = handle.send;
        botHandle = handle;
        handle.start();
        if (store.kvGet<boolean>(KV_TICKER) === null) store.kvSet(KV_TICKER, false);

        timer = setInterval(() => {
          if (draining || !sendFn) return;
          draining = true;
          void deliverOutboxOnce(store, sendFn)
            .catch((err: unknown) => {
              store.audit("daemon", "outbox.tick_error", "daemon", "card", { error: String(err) });
            })
            .finally(() => {
              draining = false;
            });
        }, DELIVER_INTERVAL_MS);
        store.audit("daemon", "telegram.started", "daemon", "card", { forum: config.telegram.forum_mode });
      },
      async stop(): Promise<void> {
        if (timer) clearInterval(timer);
        timer = null;
        const handle = botHandle;
        botHandle = null;
        sendFn = null;
        if (handle) await handle.stop();
      },
    },
  };
}

export { renderEscalation, renderDigestMessage } from "./render.ts";
export { deliverOutboxOnce, backoffSeconds } from "./outbox.ts";
export { handleCallback, handleCommand, routeIncomingMessage } from "./handlers.ts";
export type { HandlerDeps } from "./handlers.ts";
export type { MessageSpec, TelegramTarget, TelegramSendFn } from "./types.ts";
