import { buildWsUrl, getAuthToken } from "./utils.js";
import { CONSTANTS } from "./constants.js";

const SESSION_STORAGE_PREFIX = "codex_terminal_session_id:";
const SESSION_STORAGE_TS_PREFIX = "codex_terminal_session_ts:";

const RECONNECT_MAX_ATTEMPTS = 3;
const RECONNECT_STABLE_CONNECTION_MS = 15_000;
const WS_HEARTBEAT_INTERVAL_MS = 20_000;
const WS_HEARTBEAT_STALL_TIMEOUT_MS = 60_000;

function base64UrlEncode(value: string): string | null {
  if (!value) return null;
  try {
    const bytes = new TextEncoder().encode(value);
    let binary = "";
    bytes.forEach((b) => {
      binary += String.fromCharCode(b);
    });
    const base64 = btoa(binary);
    return base64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  } catch (_err) {
    return null;
  }
}

export function sessionKey(repoKey: string): string {
  return `${SESSION_STORAGE_PREFIX}${repoKey}`;
}

export function sessionTimestampKey(repoKey: string): string {
  return `${SESSION_STORAGE_TS_PREFIX}${repoKey}`;
}

export function getSavedTimestamp(repoKey: string): number | null {
  const raw = localStorage.getItem(sessionTimestampKey(repoKey));
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function setSavedTimestamp(repoKey: string, stamp: number): void {
  if (!stamp) return;
  localStorage.setItem(sessionTimestampKey(repoKey), String(stamp));
}

export function clearSavedTimestamp(repoKey: string): void {
  localStorage.removeItem(sessionTimestampKey(repoKey));
}

export function isSessionStale(
  lastActiveAt: number | null,
  idleTimeoutSeconds: number | null
): boolean {
  if (lastActiveAt === null || lastActiveAt === undefined) return false;
  if (idleTimeoutSeconds === null || idleTimeoutSeconds === undefined) return false;
  if (typeof idleTimeoutSeconds !== "number" || idleTimeoutSeconds <= 0) return false;
  return Date.now() - lastActiveAt > idleTimeoutSeconds * 1000;
}

export function getSavedSessionId(
  repoKey: string,
  idleTimeoutSeconds: number | null
): string | null {
  const scoped = localStorage.getItem(sessionKey(repoKey));
  if (scoped) {
    const lastActiveAt = getSavedTimestamp(repoKey);
    if (isSessionStale(lastActiveAt, idleTimeoutSeconds)) {
      clearSavedSessionId(repoKey);
      return null;
    }
    return scoped;
  }
  return null;
}

export function setSavedSessionId(repoKey: string, sessionId: string): void {
  if (!sessionId) return;
  localStorage.setItem(sessionKey(repoKey), sessionId);
  setSavedTimestamp(repoKey, Date.now());
}

export function clearSavedSessionId(repoKey: string): void {
  localStorage.removeItem(sessionKey(repoKey));
  clearSavedTimestamp(repoKey);
}

export function markSessionActive(repoKey: string): void {
  setSavedTimestamp(repoKey, Date.now());
}

export interface ConnectQueryOptions {
  mode: string;
  terminalDebug: boolean;
  isAttach: boolean;
  savedSessionId: string | null;
  agent: string | null;
  profile: string | null;
  model: string | null;
  reasoning: string | null;
}

export function buildConnectQuery(opts: ConnectQueryOptions): URLSearchParams {
  const params = new URLSearchParams();
  if (opts.mode) params.append("mode", opts.mode);
  if (opts.terminalDebug) params.append("terminal_debug", "1");
  if (!opts.isAttach) {
    if (opts.agent) params.append("agent", opts.agent);
    if (opts.profile) params.append("profile", opts.profile);
    if (opts.model) params.append("model", opts.model);
    if (opts.reasoning) params.append("reasoning", opts.reasoning);
  }
  if (opts.isAttach && opts.savedSessionId) {
    params.append("session_id", opts.savedSessionId);
  } else if (!opts.isAttach && opts.savedSessionId) {
    params.append("close_session_id", opts.savedSessionId);
  }
  return params;
}

export function createTerminalSocket(query: URLSearchParams): WebSocket {
  const qs = query.toString();
  const wsUrl = buildWsUrl(
    CONSTANTS.API.TERMINAL_ENDPOINT,
    qs ? `?${qs}` : ""
  );
  const token = getAuthToken();
  const encodedToken = token ? base64UrlEncode(token) : null;
  const protocols = encodedToken ? [`car-token-b64.${encodedToken}`] : undefined;
  const socket = protocols ? new WebSocket(wsUrl, protocols) : new WebSocket(wsUrl);
  socket.binaryType = "arraybuffer";
  return socket;
}

export { RECONNECT_MAX_ATTEMPTS, RECONNECT_STABLE_CONNECTION_MS };

export class SocketHeartbeat {
  private timer: number | null = null;
  private lastActivityAt: number | null = null;
  private debugLog: (msg: string, details?: unknown) => void;

  constructor(
    _debug: boolean,
    debugLog: (msg: string, details?: unknown) => void
  ) {
    this.debugLog = debugLog;
  }

  start(socket: WebSocket): void {
    this.stop();
    this.noteActivity();
    this.timer = window.setInterval(() => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const now = Date.now();
      const lastActivity = this.lastActivityAt;
      if (
        typeof lastActivity === "number" &&
        now - lastActivity > WS_HEARTBEAT_STALL_TIMEOUT_MS
      ) {
        this.debugLog("heartbeat stalled; closing terminal socket", {
          idleMs: now - lastActivity,
        });
        try {
          socket.close();
        } catch (_err) {
          // ignore close errors and let reconnect logic handle recovery
        }
        return;
      }
      if (
        typeof lastActivity === "number" &&
        now - lastActivity < WS_HEARTBEAT_INTERVAL_MS
      ) {
        return;
      }
      try {
        socket.send(JSON.stringify({ type: "ping" }));
      } catch (_err) {
        // ignore and rely on normal onclose handling
      }
    }, WS_HEARTBEAT_INTERVAL_MS);
  }

  stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.lastActivityAt = null;
  }

  noteActivity(): void {
    this.lastActivityAt = Date.now();
  }
}

export class ReconnectScheduler {
  private timer: number | null = null;
  attempts: number = 0;
  openedAt: number | null = null;

  cancel(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  resetAttemptsIfStable(): void {
    if (
      typeof this.openedAt === "number" &&
      Date.now() - this.openedAt >= RECONNECT_STABLE_CONNECTION_MS
    ) {
      this.attempts = 0;
    }
    this.openedAt = null;
  }

  schedule(
    callback: () => void,
    setStatus: (status: string) => void
  ): boolean {
    if (this.attempts >= RECONNECT_MAX_ATTEMPTS) {
      setStatus("Disconnected (max retries reached)");
      return false;
    }
    const delay = Math.min(1000 * Math.pow(2, this.attempts), 8000);
    setStatus(`Reconnecting in ${Math.round(delay / 1000)}s...`);
    this.attempts++;
    this.timer = window.setTimeout(callback, delay);
    return true;
  }
}

export function teardownSocket(
  socket: WebSocket | null,
  heartbeat: SocketHeartbeat
): void {
  if (socket) {
    socket.onclose = null;
    socket.onerror = null;
    socket.onmessage = null;
    socket.onopen = null;
    try {
      socket.close();
    } catch (_err) {
      // ignore
    }
  }
  heartbeat.stop();
}
