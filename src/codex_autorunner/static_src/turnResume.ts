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
