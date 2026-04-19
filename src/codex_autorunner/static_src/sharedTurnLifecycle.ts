import {
  loadPendingTurn,
  clearPendingTurn,
  type PendingTurn,
  type TurnRecoveryTracker,
  createTurnRecoveryTracker,
  DEFAULT_RECOVERY_MAX_ATTEMPTS,
} from "./turnResume.js";
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

export {
  type TurnRecoveryTracker,
  createTurnRecoveryTracker,
  DEFAULT_RECOVERY_MAX_ATTEMPTS,
} from "./turnResume.js";

/**
 * Unified Active-Turn Surface Policy
 *
 * | Situation            | Behavior                                          |
 * |----------------------|---------------------------------------------------|
 * | Turn running         | Abort controller + fire-and-forget server         |
 * | + user sends         | interrupt + clear pending, then immediately send  |
 * |                      | the new message.                                  |
 * | Turn running         | Abort controller + interrupt server + clear        |
 * | + user cancels       | pending. Show "Cancelled" status.                  |
 * | Recovery pending     | Retry up to max attempts. On stale: show error     |
 * | + max exceeded       | with "retry or new thread" guidance.               |
 * | Recovery pending     | Clear pending, return to idle.                     |
 * | + user discards      |                                                   |
 */
export const ACTIVE_TURN_RECOVERY_STALE_MESSAGE =
  "Could not recover previous turn. Send a new message to retry or start a new thread.";

export interface CancelActiveTurnOptions {
  abortController(): void;
  turnEventsCtrl: { abort(): void };
  interruptServer?(): Promise<unknown>;
  clearPending?(): void;
}

export function cancelActiveTurnSync(options: CancelActiveTurnOptions): void {
  options.abortController();
  options.turnEventsCtrl.abort();
  options.clearPending?.();
  if (options.interruptServer) {
    void options.interruptServer().catch(() => {});
  }
}

export interface ScheduleRecoveryRetryOptions {
  tracker: TurnRecoveryTracker;
  retryFn: () => Promise<void>;
  onStale?: () => void;
  intervalMs?: number;
}

export function scheduleRecoveryRetry(opts: ScheduleRecoveryRetryOptions): void {
  const { tracker, retryFn, onStale, intervalMs = 1000 } = opts;
  if (tracker.phase !== "recovering") return;
  if (!tracker.tick()) {
    onStale?.();
    return;
  }
  window.setTimeout(() => void retryFn(), intervalMs);
}

export const __turnRecoveryPolicyTest = {
  createTurnRecoveryTracker,
  cancelActiveTurnSync,
  scheduleRecoveryRetry,
  ACTIVE_TURN_RECOVERY_STALE_MESSAGE,
  DEFAULT_RECOVERY_MAX_ATTEMPTS,
};
