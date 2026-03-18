const decoder = new TextDecoder();

export function parseMaybeJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

export interface StreamEventHandler {
  onStatus?(status: string): void;
  onToken?(token: string): void;
  onTokenUsage?(percentRemaining: number, usage: Record<string, unknown>): void;
  onUpdate?(payload: Record<string, unknown>): void;
  onEvent?(event: unknown): void;
  onError?(message: string): void;
  onInterrupted?(message: string): void;
  onDone?(): void;
}

export function handleStreamEvent(
  event: string,
  rawData: string,
  handlers: StreamEventHandler
): void {
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
      const payload = typeof parsed === "object" && parsed !== null ? parsed : {};
      handlers.onUpdate?.(payload);
      break;
    }

    case "event":
    case "app-server": {
      handlers.onEvent?.(parsed);
      break;
    }

    case "error": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed.detail as string) || (parsed.error as string) || rawData || "Stream error")
          : rawData || "Stream error";
      handlers.onError?.(message);
      break;
    }

    case "interrupted": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed.detail as string) || rawData || "Stream interrupted")
          : rawData || "Stream interrupted";
      handlers.onInterrupted?.(message);
      break;
    }

    case "done":
    case "finish": {
      handlers.onDone?.();
      break;
    }

    default: {
      if (typeof parsed === "object" && parsed !== null) {
        const messageObj = parsed as Record<string, unknown>;
        if (messageObj.method || messageObj.message) {
          handlers.onEvent?.(parsed);
        }
      }
      break;
    }
  }
}

export function extractContextRemainingPercent(usage: unknown): number | null {
  if (!usage || typeof usage !== "object") return null;
  const payload = usage as Record<string, unknown>;
  const totalRaw = payload.totalTokens ?? payload.total ?? payload.total_tokens;
  const contextRaw =
    payload.modelContextWindow ?? payload.contextWindow ?? payload.model_context_window;

  const totalTokens = typeof totalRaw === "number" ? totalRaw : Number(totalRaw);
  const contextWindow =
    typeof contextRaw === "number" && Number.isFinite(contextRaw)
      ? contextRaw
      : Number(contextRaw);

  if (!Number.isFinite(totalTokens) || !Number.isFinite(contextWindow) || contextWindow <= 0) {
    return null;
  }

  const percentRemaining = Math.round(((contextWindow - totalTokens) / contextWindow) * 100);
  return Math.max(0, Math.min(100, percentRemaining));
}

export async function readEventStream(
  res: Response,
  handler: (event: string, data: string) => void
): Promise<void> {
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
      handler(event, dataLines.join("\n"));
    }
  }
}
