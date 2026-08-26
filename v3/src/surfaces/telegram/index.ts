/**
 * WS-D owns src/surfaces/telegram/: grammY bot (escalation messages with buttons,
 * reply routing, commands, pinned status lines) + the outbox deliverer loop.
 * Scaffold stub: enqueues to outbox; deliverer no-ops unless telegram.enabled.
 */
import type { Store } from "../../store/db.ts";
import type { CarConfig } from "../../config/config.ts";
import type { ActionBus, ChannelPort, EscalationMessage, Loop, MemoryWriter } from "../../ports.ts";

export interface TelegramChannel extends ChannelPort {
  loop: Loop;
}

export function createTelegram(
  store: Store,
  config: CarConfig,
  _actions: ActionBus,
  _memoryWriter: MemoryWriter,
): TelegramChannel {
  return {
    sendEscalation(msg: EscalationMessage) {
      store.enqueueOutbox("telegram", { kind: "escalation", escalationId: msg.escalationId }, msg);
    },
    sendNotify(text: string, carSessionId?: string) {
      store.enqueueOutbox("telegram", { kind: "notify", carSessionId: carSessionId ?? null }, { text });
    },
    sendDigest(markdown: string) {
      store.enqueueOutbox("telegram", { kind: "digest" }, { markdown });
    },
    loop: {
      name: "telegram",
      start() {
        if (!config.telegram.enabled) return;
        // Real bot + outbox deliverer implemented by WS-D.
      },
      stop() {},
    },
  };
}
