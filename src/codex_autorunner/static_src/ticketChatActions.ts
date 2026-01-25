/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, flash } from "./utils.js";
import { performTicketChatRequest } from "./ticketChatStream.js";
import { publish } from "./bus.js";

export type TicketChatStatus = "idle" | "running" | "done" | "error" | "interrupted";

export interface TicketDraft {
  content: string;
  patch: string;
  agentMessage: string;
  createdAt: string;
  baseHash: string;
}

/**
 * Represents a real-time event from the agent (thinking, tool calls, commands, etc.)
 * These are transient updates shown during processing.
 */
export interface TicketChatEvent {
  id: string;
  title: string;       // "Thinking", "Tool", "Command", "File change", etc.
  summary: string;     // The content/description
  detail: string;      // Optional detail (e.g., exit code)
  kind: string;        // "thinking", "tool", "command", "file", "output", "error", "status"
  time: number;        // Timestamp in ms
  itemId: string | null;
  method: string;      // Original event method name
}

/**
 * Represents a message in the chat history (user prompts and assistant responses).
 * Unlike events, messages persist after the request completes.
 */
export interface TicketChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  time: string;        // ISO timestamp
  isFinal: boolean;    // True for final assistant responses, false for intermediate
}

export interface TicketChatState {
  status: TicketChatStatus;
  ticketIndex: number | null;
  error: string;
  streamText: string;
  statusText: string;
  controller: AbortController | null;
  draft: TicketDraft | null;
  // Rich chat experience: events and message history
  events: TicketChatEvent[];
  messages: TicketChatMessage[];
  eventItemIndex: Record<string, number>;  // Maps itemId to event index for delta updates
  eventsExpanded: boolean;
}

// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;

// Global state for ticket chat
export const ticketChatState: TicketChatState = {
  status: "idle",
  ticketIndex: null,
  error: "",
  streamText: "",
  statusText: "",
  controller: null,
  draft: null,
  events: [],
  messages: [],
  eventItemIndex: {},
  eventsExpanded: false,
};

export function getTicketChatElements() {
  return {
    input: document.getElementById("ticket-chat-input") as HTMLTextAreaElement | null,
    sendBtn: document.getElementById("ticket-chat-send") as HTMLButtonElement | null,
    voiceBtn: document.getElementById("ticket-chat-voice") as HTMLButtonElement | null,
    cancelBtn: document.getElementById("ticket-chat-cancel") as HTMLButtonElement | null,
    statusEl: document.getElementById("ticket-chat-status") as HTMLElement | null,
    streamEl: document.getElementById("ticket-chat-stream") as HTMLElement | null,
    // Content area elements - mutually exclusive with patch preview
    contentTextarea: document.getElementById("ticket-editor-content") as HTMLTextAreaElement | null,
    contentToolbar: document.getElementById("ticket-editor-toolbar") as HTMLElement | null,
    // Patch preview elements - mutually exclusive with content area
    patchMain: document.getElementById("ticket-patch-main") as HTMLElement | null,
    patchBody: document.getElementById("ticket-patch-body") as HTMLElement | null,
    patchStatus: document.getElementById("ticket-patch-status") as HTMLElement | null,
    applyBtn: document.getElementById("ticket-patch-apply") as HTMLButtonElement | null,
    discardBtn: document.getElementById("ticket-patch-discard") as HTMLButtonElement | null,
    agentSelect: document.getElementById("ticket-chat-agent-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("ticket-chat-model-select") as HTMLSelectElement | null,
    reasoningSelect: document.getElementById("ticket-chat-reasoning-select") as HTMLSelectElement | null,
    // Rich chat experience: events and messages
    eventsMain: document.getElementById("ticket-chat-events") as HTMLElement | null,
    eventsList: document.getElementById("ticket-chat-events-list") as HTMLElement | null,
    eventsCount: document.getElementById("ticket-chat-events-count") as HTMLElement | null,
    eventsToggle: document.getElementById("ticket-chat-events-toggle") as HTMLButtonElement | null,
    messagesEl: document.getElementById("ticket-chat-messages") as HTMLElement | null,
  };
}

export function resetTicketChatState(): void {
  ticketChatState.status = "idle";
  ticketChatState.error = "";
  ticketChatState.streamText = "";
  ticketChatState.statusText = "";
  ticketChatState.controller = null;
  // Note: events are cleared at the start of each new request, not here
  // Messages persist across requests within the same ticket
}

/**
 * Clear events at the start of a new request.
 * Events are transient (thinking/tool calls) and reset each turn.
 */
export function clearTicketEvents(): void {
  ticketChatState.events = [];
  ticketChatState.eventItemIndex = {};
}

/**
 * Add a user message to the chat history.
 */
export function addUserMessage(content: string): void {
  ticketChatState.messages.push({
    id: `user-${Date.now()}`,
    role: "user",
    content,
    time: new Date().toISOString(),
    isFinal: true,
  });
}

/**
 * Add an assistant message to the chat history.
 */
export function addAssistantMessage(content: string, isFinal = true): void {
  ticketChatState.messages.push({
    id: `assistant-${Date.now()}`,
    role: "assistant",
    content,
    time: new Date().toISOString(),
    isFinal,
  });
}

export function setTicketIndex(index: number | null): void {
  const changed = ticketChatState.ticketIndex !== index;
  ticketChatState.ticketIndex = index;
  ticketChatState.draft = null;
  resetTicketChatState();
  // Clear chat history when switching tickets
  if (changed) {
    ticketChatState.messages = [];
    clearTicketEvents();
  }
}

export function renderTicketChat(): void {
  const els = getTicketChatElements();
  
  // Update status pill
  if (els.statusEl) {
    const status = ticketChatState.status;
    let displayText: string = status;
    
    if (ticketChatState.error) {
      displayText = "error";
      els.statusEl.classList.add("error");
    } else {
      els.statusEl.classList.remove("error");
      if (ticketChatState.statusText) {
        displayText = ticketChatState.statusText;
      }
    }
    
    els.statusEl.textContent = displayText;
    els.statusEl.classList.toggle("running", status === "running");
  }

  // Show/hide cancel button
  if (els.cancelBtn) {
    els.cancelBtn.classList.toggle("hidden", ticketChatState.status !== "running");
  }

  // The streamEl now contains events and messages sections.
  // Show the stream container when there are events, messages, or running.
  if (els.streamEl) {
    const hasContent =
      ticketChatState.events.length > 0 ||
      ticketChatState.messages.length > 0 ||
      ticketChatState.status === "running";
    els.streamEl.classList.toggle("hidden", !hasContent);
  }

  // MUTUALLY EXCLUSIVE: Show either the content editor OR the patch preview, never both.
  // This prevents confusion about which view is the "current" state.
  const hasDraft = !!ticketChatState.draft;

  // Hide content area when showing patch preview
  if (els.contentTextarea) {
    els.contentTextarea.classList.toggle("hidden", hasDraft);
  }
  if (els.contentToolbar) {
    els.contentToolbar.classList.toggle("hidden", hasDraft);
  }

  // Show patch preview only when there's a draft
  if (els.patchMain) {
    els.patchMain.classList.toggle("hidden", !hasDraft);
    if (hasDraft) {
      if (els.patchBody) {
        els.patchBody.textContent = ticketChatState.draft!.patch || "(no changes)";
      }
      if (els.patchStatus) {
        els.patchStatus.textContent = ticketChatState.draft!.agentMessage || "";
      }
    }
  }
}

export async function sendTicketChat(): Promise<void> {
  const els = getTicketChatElements();
  const message = (els.input?.value || "").trim();
  
  if (!message) {
    ticketChatState.error = "Enter a message to send.";
    renderTicketChat();
    return;
  }

  if (ticketChatState.status === "running") {
    ticketChatState.error = "Ticket chat already running.";
    renderTicketChat();
    flash("Ticket chat already running", "error");
    return;
  }

  if (ticketChatState.ticketIndex == null) {
    ticketChatState.error = "No ticket selected.";
    renderTicketChat();
    return;
  }

  resetTicketChatState();
  ticketChatState.status = "running";
  ticketChatState.statusText = "queued";
  ticketChatState.controller = new AbortController();

  renderTicketChat();
  if (els.input) {
    els.input.value = "";
  }

  const agent = els.agentSelect?.value || "codex";
  const model = els.modelSelect?.value || undefined;
  const reasoning = els.reasoningSelect?.value || undefined;

  try {
    await performTicketChatRequest(
      ticketChatState.ticketIndex,
      message,
      ticketChatState.controller.signal,
      {
        agent,
        model,
        reasoning,
      }
    );
    
    // Try to load any pending draft
    await loadTicketPending(ticketChatState.ticketIndex, true);

    if (ticketChatState.status === "running") {
      ticketChatState.status = "done";
    }
  } catch (err) {
    const error = err as Error;
    if (error.name === "AbortError") {
      ticketChatState.status = "interrupted";
      ticketChatState.error = "";
    } else {
      ticketChatState.status = "error";
      ticketChatState.error = error.message || "Ticket chat failed";
    }
  } finally {
    ticketChatState.controller = null;
    renderTicketChat();
  }
}

export async function cancelTicketChat(): Promise<void> {
  if (ticketChatState.status !== "running") return;
  
  // Abort the request
  if (ticketChatState.controller) {
    ticketChatState.controller.abort();
  }

  // Send interrupt to server
  if (ticketChatState.ticketIndex != null) {
    try {
      await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/interrupt`, {
        method: "POST",
      });
    } catch (err) {
      // Ignore interrupt errors
    }
  }

  ticketChatState.status = "interrupted";
  ticketChatState.error = "";
  ticketChatState.statusText = "";
  ticketChatState.controller = null;
  renderTicketChat();
}

export async function applyTicketPatch(): Promise<void> {
  if (ticketChatState.ticketIndex == null) {
    flash("No ticket selected", "error");
    return;
  }

  if (!ticketChatState.draft) {
    flash("No draft to apply", "error");
    return;
  }

  try {
    const res = await api(
      `/api/tickets/${ticketChatState.ticketIndex}/chat/apply`,
      { method: "POST" }
    ) as { content?: string };

    ticketChatState.draft = null;
    flash("Draft applied");
    
    // Notify that tickets changed
    publish("tickets:updated", {});
    
    // Update the editor textarea if content is returned
    if (res.content) {
      const textarea = document.getElementById("ticket-editor-content") as HTMLTextAreaElement | null;
      if (textarea) {
        textarea.value = res.content;
      }
    }
  } catch (err) {
    const error = err as Error;
    flash(error.message || "Failed to apply draft", "error");
  } finally {
    renderTicketChat();
  }
}

export async function discardTicketPatch(): Promise<void> {
  if (ticketChatState.ticketIndex == null) {
    flash("No ticket selected", "error");
    return;
  }

  try {
    await api(
      `/api/tickets/${ticketChatState.ticketIndex}/chat/discard`,
      { method: "POST" }
    );

    ticketChatState.draft = null;
    flash("Draft discarded");
  } catch (err) {
    const error = err as Error;
    flash(error.message || "Failed to discard draft", "error");
  } finally {
    renderTicketChat();
  }
}

export async function loadTicketPending(index: number, silent = false): Promise<void> {
  try {
    const res = await api(`/api/tickets/${index}/chat/pending`, { method: "GET" }) as {
      patch?: string;
      content?: string;
      agent_message?: string;
      created_at?: string;
      base_hash?: string;
    };

    ticketChatState.draft = {
      patch: res.patch || "",
      content: res.content || "",
      agentMessage: res.agent_message || "",
      createdAt: res.created_at || "",
      baseHash: res.base_hash || "",
    };

    if (!silent) {
      flash("Loaded pending draft");
    }
  } catch (err) {
    const error = err as Error;
    const message = error?.message || "";
    
    if (message.includes("No pending")) {
      ticketChatState.draft = null;
      if (!silent) {
        flash("No pending draft");
      }
    } else if (!silent) {
      flash(message || "Failed to load pending draft", "error");
    }
  } finally {
    renderTicketChat();
  }
}
