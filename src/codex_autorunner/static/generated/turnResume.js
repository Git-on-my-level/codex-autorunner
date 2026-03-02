// GENERATED FILE - do not edit directly. Source: static_src/
export function loadPendingTurn(key) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw)
            return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object")
            return null;
        if (!parsed.clientTurnId || !parsed.message || !parsed.startedAtMs)
            return null;
        return parsed;
    }
    catch {
        return null;
    }
}
export function savePendingTurn(key, turn) {
    try {
        localStorage.setItem(key, JSON.stringify(turn));
    }
    catch {
        // ignore
    }
}
export function clearPendingTurn(key) {
    try {
        localStorage.removeItem(key);
    }
    catch {
        // ignore
    }
}
