/**
 * Canonical Pending Turn Storage
 *
 * This module provides the canonical localStorage contract for pending turn recovery
 * across all chat surfaces (ticket, contextspace, PMA).
 *
 * Namespace conventions (localStorage keys):
 * - Ticket chat: `car.ticketChat.pending.${ticketChatKey}` or `car.ticketChat.pending.${index}`
 * - Contextspace: `car.contextspace.pendingTurn`
 * - PMA: `car.pma.pendingTurn`
 *
 * Usage pattern:
 * - Call savePendingTurn(key, turn) when starting a chat turn
 * - Call loadPendingTurn(key) on page load to check for recovery
 * - Call clearPendingTurn(key) when turn completes or is cancelled
 */
export interface PendingTurn {
  clientTurnId: string;
  message: string;
  startedAtMs: number;
  target?: string;
}

export function loadPendingTurn(key: string): PendingTurn | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingTurn>;
    if (!parsed || typeof parsed !== "object") return null;
    if (!parsed.clientTurnId || !parsed.message || !parsed.startedAtMs) return null;
    return parsed as PendingTurn;
  } catch {
    return null;
  }
}

export function savePendingTurn(key: string, turn: PendingTurn): void {
  try {
    localStorage.setItem(key, JSON.stringify(turn));
  } catch {
    // ignore
  }
}

export function clearPendingTurn(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

export type TurnRecoveryPhase = "recovering" | "stale";

export interface TurnRecoveryTracker {
  readonly phase: TurnRecoveryPhase;
  readonly attempts: number;
  readonly maxAttempts: number;
  tick(): boolean;
}

export const DEFAULT_RECOVERY_MAX_ATTEMPTS = 30;

export function createTurnRecoveryTracker(
  maxAttempts?: number
): TurnRecoveryTracker {
  let phase: TurnRecoveryPhase = "recovering";
  let attempts = 0;
  const max = maxAttempts ?? DEFAULT_RECOVERY_MAX_ATTEMPTS;
  return {
    get phase() {
      return phase;
    },
    get attempts() {
      return attempts;
    },
    get maxAttempts() {
      return max;
    },
    tick() {
      if (phase !== "recovering") return false;
      attempts += 1;
      if (attempts >= max) {
        phase = "stale";
        return false;
      }
      return true;
    },
  };
}
