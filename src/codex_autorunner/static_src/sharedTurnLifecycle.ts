import { loadPendingTurn, clearPendingTurn, type PendingTurn } from "./turnResume.js";
import { streamTurnEvents } from "./fileChat.js";

export interface ManagedTurnEventsOptions {
  onEvent?(payload: unknown): void;
}

export function createTurnEventsController(): {
  current: AbortController | null;
  abort(): void;
} {
  let controller: AbortController | null = null;

  return {
    get current() {
      return controller;
    },
    set current(value: AbortController | null) {
      controller = value;
    },
    abort() {
      if (controller) {
        try {
          controller.abort();
        } catch {
          // ignore
        }
        controller = null;
      }
    },
  };
}

export function startTurnEventsStream(
  ctrl: { current: AbortController | null; abort(): void },
  meta: { agent?: string; threadId: string; turnId: string },
  options: ManagedTurnEventsOptions = {}
): void {
  const threadId = meta.threadId;
  const turnId = meta.turnId;
  if (!threadId || !turnId) return;
  ctrl.abort();
  ctrl.current = streamTurnEvents(
    {
      agent: meta.agent,
      threadId,
      turnId,
    },
    {
      onEvent: options.onEvent,
    }
  );
}

export function clearManagedTurn(
  ctrl: { abort(): void },
  pendingKey: string
): void {
  ctrl.abort();
  clearPendingTurn(pendingKey);
}

export function loadManagedPendingTurn(
  key: string
): PendingTurn | null {
  return loadPendingTurn(key);
}

export { loadPendingTurn, savePendingTurn, clearPendingTurn, type PendingTurn } from "./turnResume.js";
