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
import { readEventStream, handleStreamEvent, type StreamEventHandler } from "./streamUtils.js";

interface ChatRequestPayload {
  message: string;
  stream: boolean;
  agent?: string;
  profile?: string;
  model?: string;
  reasoning?: string;
  client_turn_id?: string;
}

interface ChatRequestOptions {
  agent?: string;
  profile?: string;
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
  ticketChatState.contextUsagePercent = null;
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
  if (options.profile) payload.profile = options.profile;
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
    await readEventStream(res, (event, data) => {
      handleStreamEvent(event, data, ticketChatStreamHandlers);
    });
  } else {
    // Non-streaming response
    const responsePayload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyTicketChatResult(responsePayload);
  }
}

const ticketChatStreamHandlers: StreamEventHandler = {
  onStatus(status) {
    ticketChatState.statusText = status;
    renderTicketChat();
    renderTicketEvents();
  },

  onToken(token) {
    ticketChatState.streamText = (ticketChatState.streamText || "") + token;
    if (!ticketChatState.statusText || ticketChatState.statusText === "queued") {
      ticketChatState.statusText = "responding";
    }
    renderTicketChat();
  },

  onTokenUsage(percentRemaining) {
    ticketChatState.contextUsagePercent = percentRemaining;
    renderTicketChat();
  },

  onUpdate(payload) {
    applyTicketChatResult(payload);
  },

  onEvent(event) {
    applyTicketEvent(event);
    renderTicketEvents();
  },

  onError(message) {
    ticketChatState.status = "error";
    ticketChatState.error = message;
    addAssistantMessage(`Error: ${message}`, true);
    renderTicketChat();
    renderTicketMessages();
    throw new Error(message);
  },

  onInterrupted(message) {
    ticketChatState.status = "interrupted";
    ticketChatState.error = "";
    ticketChatState.statusText = message;
    addAssistantMessage("Request interrupted", true);
    renderTicketChat();
    renderTicketMessages();
  },

  onDone() {
    ticketChatState.status = "done";
    renderTicketChat();
    renderTicketMessages();
    renderTicketEvents();
  },
};
