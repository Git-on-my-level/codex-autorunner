import { resolvePath, getAuthToken, api } from "./utils.js";
import { extractContextRemainingPercent, readEventStream, parseMaybeJson } from "./streamUtils.js";

export interface FileChatOptions {
  agent?: string;
  model?: string;
  reasoning?: string;
  clientTurnId?: string;
  basePath?: string;
}

export interface FileDraft {
  target: string;
  content: string;
  patch: string;
  agent_message?: string;
  created_at?: string;
  base_hash?: string;
  current_hash?: string;
  is_stale?: boolean;
}

export interface FileChatUpdate {
  status?: string;
  message?: string;
  agent_message?: string;
  patch?: string;
  content?: string;
  has_draft?: boolean;
  hasDraft?: boolean;
  created_at?: string;
  base_hash?: string;
  current_hash?: string;
  is_stale?: boolean;
  raw_events?: unknown[];
  target?: string;
  detail?: string;
  error?: string;
}

export interface FileChatHandlers {
  onStatus?(status: string): void;
  onToken?(token: string): void;
  onTokenUsage?(percentRemaining: number, usage: Record<string, unknown>): void;
  onUpdate?(update: FileChatUpdate): void;
  onEvent?(event: unknown): void;
  onError?(message: string): void;
  onInterrupted?(message: string): void;
  onDone?(): void;
}

export async function sendFileChat(
  target: string,
  message: string,
  controller: AbortController,
  handlers: FileChatHandlers = {},
  options: FileChatOptions = {}
): Promise<void> {
  const endpoint = resolvePath(options.basePath || "/api/file-chat");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const payload: Record<string, unknown> = {
    target,
    message,
    stream: true,
  };
  if (options.clientTurnId) payload.client_turn_id = options.clientTurnId;
  if (options.agent) payload.agent = options.agent;
  if (options.model) payload.model = options.model;
  if (options.reasoning) payload.reasoning = options.reasoning;

  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal: controller.signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>;
      detail =
        (parsed.detail as string) || (parsed.error as string) || (parsed.message as string) || text;
    } catch {
      // ignore
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    await readFileChatStream(res, handlers);
  } else {
    const responsePayload = contentType.includes("application/json") ? await res.json() : await res.text();
    handlers.onUpdate?.(responsePayload as FileChatUpdate);
    handlers.onDone?.();
  }
}

async function readFileChatStream(res: Response, handlers: FileChatHandlers): Promise<void> {
  await readEventStream(res, (event, raw) => handleStreamEvent(event, raw, handlers));
}

function handleStreamEvent(event: string, rawData: string, handlers: FileChatHandlers): void {
  const parsed = parseMaybeJson(rawData) as Record<string, unknown> | string;
  switch (event) {
    case "status": {
      const status = typeof parsed === "string" ? parsed : (parsed.status as string) || "";
      handlers.onStatus?.(status);
      break;
    }
    case "token": {
      const token =
        typeof parsed === "string"
          ? parsed
          : (parsed.token as string) || (parsed.text as string) || rawData || "";
      handlers.onToken?.(token);
      break;
    }
    case "token_usage": {
      if (typeof parsed === "object" && parsed !== null) {
        const usage = parsed as Record<string, unknown>;
        const percent = extractContextRemainingPercent(usage);
        if (percent !== null) {
          handlers.onTokenUsage?.(percent, usage);
        }
      }
      break;
    }
    case "update": {
      handlers.onUpdate?.(parsed as FileChatUpdate);
      break;
    }
    case "event":
    case "app-server": {
      handlers.onEvent?.(parsed);
      break;
    }
    case "error": {
      const msg =
        typeof parsed === "object" && parsed !== null
          ? ((parsed.detail as string) || (parsed.error as string) || rawData || "File chat failed")
          : rawData || "File chat failed";
      handlers.onError?.(msg);
      break;
    }
    case "interrupted": {
      const msg =
        typeof parsed === "object" && parsed !== null
          ? ((parsed.detail as string) || rawData || "File chat interrupted")
          : rawData || "File chat interrupted";
      handlers.onInterrupted?.(msg);
      break;
    }
    case "done":
    case "finish": {
      handlers.onDone?.();
      break;
    }
    default:
      // treat unknown as event for visibility
      handlers.onEvent?.(parsed);
      break;
  }
}

export async function fetchPendingDraft(target: string): Promise<FileDraft | null> {
  try {
    const res = (await api(`/api/file-chat/pending?target=${encodeURIComponent(target)}`)) as Record<string, unknown>;
    if (!res || typeof res !== "object") return null;
    return {
      target: (res.target as string) || target,
      content: (res.content as string) || "",
      patch: (res.patch as string) || "",
      agent_message: (res.agent_message as string) || undefined,
      created_at: (res.created_at as string) || undefined,
      base_hash: (res.base_hash as string) || undefined,
      current_hash: (res.current_hash as string) || undefined,
      is_stale: Boolean(res.is_stale),
    };
  } catch {
    return null;
  }
}

export async function applyDraft(
  target: string,
  options: { force?: boolean } = {}
): Promise<{ content: string; agent_message?: string }> {
  const res = (await api("/api/file-chat/apply", {
    method: "POST",
    body: { target, force: Boolean(options.force) },
  })) as Record<string, unknown>;
  return {
    content: (res.content as string) || "",
    agent_message: (res.agent_message as string) || undefined,
  };
}

export async function discardDraft(target: string): Promise<{ content: string }> {
  const res = (await api("/api/file-chat/discard", {
    method: "POST",
    body: { target },
  })) as Record<string, unknown>;
  return {
    content: (res.content as string) || "",
  };
}

export async function interruptFileChat(target: string): Promise<void> {
  await api("/api/file-chat/interrupt", { method: "POST", body: { target } });
}

export interface ActiveTurnPayload {
  active?: boolean;
  current?: Record<string, unknown>;
  last_result?: Record<string, unknown>;
}

export interface TurnEventMeta {
  agent?: string;
  threadId: string;
  turnId: string;
  basePath?: string;
}

export function newClientTurnId(prefix = "filechat"): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export async function fetchActiveFileChat(
  clientTurnId: string,
  basePath = "/api/file-chat/active"
): Promise<ActiveTurnPayload> {
  const suffix = clientTurnId ? `?client_turn_id=${encodeURIComponent(clientTurnId)}` : "";
  const path = `${basePath}${suffix}`;
  try {
    const res = (await api(path)) as ActiveTurnPayload;
    return res || {};
  } catch {
    return {};
  }
}

export function streamTurnEvents(
  meta: TurnEventMeta,
  handlers: { onEvent?(payload: unknown): void; onError?(msg: string): void } = {}
): AbortController | null {
  if (!meta.threadId || !meta.turnId) return null;
  const ctrl = new AbortController();
  const token = getAuthToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  const url = resolvePath(
    `${meta.basePath || "/api/file-chat/turns"}/${encodeURIComponent(meta.turnId)}/events?thread_id=${encodeURIComponent(
      meta.threadId
    )}&agent=${encodeURIComponent(meta.agent || "codex")}`
  );
  void (async () => {
    try {
      const res = await fetch(url, { method: "GET", headers, signal: ctrl.signal });
      if (!res.ok) {
        handlers.onError?.("Failed to stream events");
        return;
      }
      const contentType = res.headers.get("content-type") || "";
      if (!contentType.includes("text/event-stream")) return;
      await readEventStream(res, (event, raw) => {
        if (event === "app-server" || event === "event") {
          handlers.onEvent?.(parseMaybeJson(raw));
        }
      });
    } catch (err) {
      handlers.onError?.((err as Error).message || "Event stream failed");
    }
  })();
  return ctrl;
}
