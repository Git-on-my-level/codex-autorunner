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
