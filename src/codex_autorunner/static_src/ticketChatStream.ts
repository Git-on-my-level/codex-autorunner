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
  applyTicketChatResult,
} from "./ticketChatActions.js";
import { applyTicketEvent, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js";
import { readEventStream, parseMaybeJson } from "./streamUtils.js";

interface ChatRequestPayload {
  message: string;
  stream: boolean;
  agent?: string;
  model?: string;
  reasoning?: string;
  client_turn_id?: string;
}

interface ChatRequestOptions {
  agent?: string;
  model?: string;
  reasoning?: string;
  clientTurnId?: string;
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
  if (options.clientTurnId) payload.client_turn_id = options.clientTurnId;

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
    await readEventStream(res, (event, data) => handleTicketStreamEvent(event, data));
  } else {
    // Non-streaming response
    const responsePayload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyTicketChatResult(responsePayload);
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

    case "token_usage": {
      // Token usage events - context window usage
      if (typeof parsed === "object" && parsed !== null) {
        const usage = parsed as Record<string, unknown>;
        const totalTokens = usage.totalTokens as number | undefined;
        const modelContextWindow = usage.modelContextWindow as number | undefined;
        if (totalTokens !== undefined && modelContextWindow !== undefined && modelContextWindow > 0) {
          const percentRemaining = Math.round(((modelContextWindow - totalTokens) / modelContextWindow) * 100);
          const percentRemainingClamped = Math.max(0, Math.min(100, percentRemaining));
          // Store context usage for display in chat UI
          ticketChatState.contextUsagePercent = percentRemainingClamped;
          renderTicketChat();
        }
      }
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
