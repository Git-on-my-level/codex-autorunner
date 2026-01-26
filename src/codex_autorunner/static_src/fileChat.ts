import { resolvePath, getAuthToken, api } from "./utils.js";

export interface FileChatOptions {
  agent?: string;
  model?: string;
  reasoning?: string;
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
  onUpdate?(update: FileChatUpdate): void;
  onEvent?(event: unknown): void;
  onError?(message: string): void;
  onInterrupted?(message: string): void;
  onDone?(): void;
}

const decoder = new TextDecoder();

function parseMaybeJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

export async function sendFileChat(
  target: string,
  message: string,
  controller: AbortController,
  handlers: FileChatHandlers = {},
  options: FileChatOptions = {}
): Promise<void> {
  const endpoint = resolvePath("/api/file-chat");
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
  if (!res.body) throw new Error("Streaming not supported in this browser");

  const reader = res.body.getReader();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines: string[] = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      });
      if (!dataLines.length) continue;
      const rawData = dataLines.join("\n");
      handleStreamEvent(event, rawData, handlers);
    }
  }
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
