/**
 * Ticket Chat Stream - handles SSE streaming for ticket chat
 */
import { resolvePath, getAuthToken } from "./utils.js";
import {
  ticketChatState,
  renderTicketChat,
  clearTicketEvents,
  addUserMessage,
  addAssistantMessage,
} from "./ticketChatActions.js";
import { applyTicketEvent, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js";

interface ChatRequestPayload {
  message: string;
  stream: boolean;
  agent?: string;
  model?: string;
  reasoning?: string;
}

interface ChatRequestOptions {
  agent?: string;
  model?: string;
  reasoning?: string;
}

const decoder = new TextDecoder();

function parseMaybeJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

export async function performTicketChatRequest(
  ticketIndex: number,
  message: string,
  signal: AbortSignal,
  options: ChatRequestOptions = {}
): Promise<void> {
  // Clear events from previous request and add user message to history
  clearTicketEvents();
  addUserMessage(message);
  // Render both chat (for container visibility) and messages
  renderTicketChat();
  renderTicketMessages();

  const endpoint = resolvePath(`/api/tickets/${ticketIndex}/chat`);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const payload: ChatRequestPayload = {
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
    signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>;
      detail = (parsed.detail as string) || (parsed.error as string) || text;
    } catch {
      // ignore parse errors
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    await readTicketChatStream(res);
  } else {
    // Non-streaming response
    const responsePayload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyTicketChatResult(responsePayload);
  }
}

async function readTicketChatStream(res: Response): Promise<void> {
  if (!res.body) throw new Error("Streaming not supported in this browser");
  
  const reader = res.body.getReader();
  let buffer = "";
  let escapedNewlines = false;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;

    const decoded = decoder.decode(value, { stream: true });
    
    // Handle escaped newlines
    if (!escapedNewlines) {
      const combined = buffer + decoded;
      if (!combined.includes("\n") && combined.includes("\\n")) {
        escapedNewlines = true;
        buffer = buffer.replace(/\\n(?=event:|data:|\\n)/g, "\n");
      }
    }
    
    buffer += escapedNewlines
      ? decoded.replace(/\\n(?=event:|data:|\\n)/g, "\n")
      : decoded;

    // Split on double newlines (SSE message delimiter)
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
        } else if (line.trim()) {
          dataLines.push(line);
        }
      });

      if (dataLines.length === 0) continue;
      const data = dataLines.join("\n");
      handleTicketStreamEvent(event, data);
    }
  }
}

function handleTicketStreamEvent(event: string, rawData: string): void {
  const parsed = parseMaybeJson(rawData) as Record<string, unknown> | string;

  switch (event) {
    case "status": {
      const status =
        typeof parsed === "string"
          ? parsed
          : ((parsed as Record<string, unknown>).status as string) || "";
      ticketChatState.statusText = status;
      renderTicketChat();
      renderTicketEvents();
      break;
    }

    case "token": {
      const token =
        typeof parsed === "string"
          ? parsed
          : ((parsed as Record<string, unknown>).token as string) ||
            ((parsed as Record<string, unknown>).text as string) ||
            rawData ||
            "";
      ticketChatState.streamText = (ticketChatState.streamText || "") + token;
      if (!ticketChatState.statusText || ticketChatState.statusText === "queued") {
        ticketChatState.statusText = "responding";
      }
      renderTicketChat();
      break;
    }

    case "update": {
      applyTicketChatResult(parsed);
      break;
    }

    case "event":
    case "app-server": {
      // App-server events (thinking, tool calls, etc.)
      applyTicketEvent(parsed);
      renderTicketEvents();
      break;
    }

    case "error": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed as Record<string, unknown>).detail as string) ||
            ((parsed as Record<string, unknown>).error as string) ||
            rawData
          : rawData || "Ticket chat failed";
      ticketChatState.status = "error";
      ticketChatState.error = String(message);
      // Add error as assistant message
      addAssistantMessage(`Error: ${message}`, true);
      renderTicketChat();
      renderTicketMessages();
      throw new Error(String(message));
    }

    case "interrupted": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed as Record<string, unknown>).detail as string) || rawData
          : rawData || "Ticket chat interrupted";
      ticketChatState.status = "interrupted";
      ticketChatState.error = "";
      ticketChatState.statusText = String(message);
      // Add interrupted message
      addAssistantMessage("Request interrupted", true);
      renderTicketChat();
      renderTicketMessages();
      break;
    }

    case "done":
    case "finish": {
      ticketChatState.status = "done";
      // Final render to ensure UI is up to date
      renderTicketChat();
      renderTicketMessages();
      renderTicketEvents();
      break;
    }

    default:
      // Unknown event - try to parse as app-server event
      if (typeof parsed === "object" && parsed !== null) {
        const messageObj = parsed as Record<string, unknown>;
        if (messageObj.method || messageObj.message) {
          applyTicketEvent(parsed);
          renderTicketEvents();
        }
      }
      break;
  }
}

function applyTicketChatResult(payload: unknown): void {
  if (!payload || typeof payload !== "object") return;

  const result = payload as Record<string, unknown>;

  if (result.status === "interrupted") {
    ticketChatState.status = "interrupted";
    ticketChatState.error = "";
    addAssistantMessage("Request interrupted", true);
    renderTicketChat();
    renderTicketMessages();
    return;
  }

  if (result.status === "error" || result.error) {
    ticketChatState.status = "error";
    ticketChatState.error =
      (result.detail as string) || (result.error as string) || "Chat failed";
    addAssistantMessage(`Error: ${ticketChatState.error}`, true);
    renderTicketChat();
    renderTicketMessages();
    return;
  }

  // Success
  ticketChatState.status = "done";

  if (result.message) {
    ticketChatState.streamText = result.message as string;
  }

  if (result.agent_message || result.agentMessage) {
    ticketChatState.statusText =
      (result.agent_message as string) || (result.agentMessage as string) || "";
  }

  // Check for draft/patch in response
  const hasDraft =
    (result.has_draft as boolean | undefined) ?? (result.hasDraft as boolean | undefined);
  if (hasDraft === false) {
    ticketChatState.draft = null;
  } else if (hasDraft === true || result.draft || result.patch || result.content) {
    ticketChatState.draft = {
      content: (result.content as string) || "",
      patch: (result.patch as string) || "",
      agentMessage:
        (result.agent_message as string) || (result.agentMessage as string) || "",
      createdAt: (result.created_at as string) || (result.createdAt as string) || "",
      baseHash: (result.base_hash as string) || (result.baseHash as string) || "",
    };
  }

  // Add assistant message from response
  const responseText =
    ticketChatState.streamText ||
    ticketChatState.statusText ||
    (ticketChatState.draft ? "Changes ready to apply" : "Done");
  if (responseText && ticketChatState.messages.length > 0) {
    // Only add if we have messages (i.e., a user message was sent)
    const lastMessage = ticketChatState.messages[ticketChatState.messages.length - 1];
    // Avoid duplicate assistant messages
    if (lastMessage.role === "user") {
      addAssistantMessage(responseText, true);
    }
  }

  renderTicketChat();
  renderTicketMessages();
  renderTicketEvents();
}
