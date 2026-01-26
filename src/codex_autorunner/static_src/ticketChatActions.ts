/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, flash, splitMarkdownFrontmatter } from "./utils.js";
import { performTicketChatRequest } from "./ticketChatStream.js";
import { renderTicketMessages } from "./ticketChatEvents.js";
import { publish } from "./bus.js";
import { createDocChat, type ChatState, type ChatStatus } from "./docChatCore.js";
import { saveTicketChatHistory } from "./ticketChatStorage.js";
import { renderDiff } from "./diffRenderer.js";

export type TicketChatStatus = ChatStatus;

export interface TicketDraft {
  content: string;
  patch: string;
  agentMessage: string;
  createdAt: string;
  baseHash: string;
}

export interface TicketChatState extends ChatState {
  ticketIndex: number | null;
  draft: TicketDraft | null;
}

// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;

export const ticketChat = createDocChat({
  idPrefix: "ticket-chat",
  storage: { keyPrefix: "car-ticket-chat-", maxMessages: 50, version: 1 },
  limits: { eventVisible: TICKET_CHAT_EVENT_LIMIT, eventMax: TICKET_CHAT_EVENT_MAX },
  styling: {
    eventClass: "ticket-chat-event",
    eventTitleClass: "ticket-chat-event-title",
    eventSummaryClass: "ticket-chat-event-summary",
    eventDetailClass: "ticket-chat-event-detail",
    eventMetaClass: "ticket-chat-event-meta",
    eventsEmptyClass: "ticket-chat-events-empty",
    eventsWaitingClass: "ticket-chat-events-waiting",
    eventsHiddenClass: "hidden",
    messagesClass: "ticket-chat-message",
    messageRoleClass: "ticket-chat-message-role",
    messageContentClass: "ticket-chat-message-content",
    messageMetaClass: "ticket-chat-message-meta",
    messageUserClass: "user",
    messageAssistantClass: "assistant",
    messageAssistantThinkingClass: "thinking",
    messageAssistantFinalClass: "final",
  },
});

// Extend state with ticket-specific fields
export const ticketChatState: TicketChatState = Object.assign(ticketChat.state, {
  ticketIndex: null,
  draft: null,
});

export function getTicketChatElements() {
  const base = ticketChat.elements;
  return {
    input: base.input,
    sendBtn: base.sendBtn,
    voiceBtn: base.voiceBtn,
    cancelBtn: base.cancelBtn,
    newThreadBtn: base.newThreadBtn,
    statusEl: base.statusEl,
    streamEl: base.streamEl,
    eventsMain: base.eventsMain,
    eventsList: base.eventsList,
    eventsCount: base.eventsCount,
    eventsToggle: base.eventsToggle,
    messagesEl: base.messagesEl,
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

export async function startNewTicketChatThread(): Promise<void> {
  if (ticketChatState.ticketIndex == null) return;

  const confirmed = window.confirm("Start a new conversation thread for this ticket?");
  if (!confirmed) return;

  try {
    const key = `ticket_chat.${ticketChatState.ticketIndex}`;
    await api(`/api/app-server/threads/reset`, {
      method: "POST",
      body: { key },
    });

    // Clear local message history
    ticketChatState.messages = [];
    saveTicketChatHistory(ticketChatState.ticketIndex, []);
    clearTicketEvents();
    
    flash("New thread started");
  } catch (err) {
    flash(`Failed to reset thread: ${(err as Error).message}`, "error");
  } finally {
    renderTicketChat();
    renderTicketMessages();
  }
}

/**
 * Clear events at the start of a new request.
 * Events are transient (thinking/tool calls) and reset each turn.
 */
export function clearTicketEvents(): void {
  ticketChat.clearEvents();
}

/**
 * Add a user message to the chat history.
 */
export function addUserMessage(content: string): void {
  ticketChat.addUserMessage(content);
}

/**
 * Add an assistant message to the chat history.
 * Prevents duplicates by checking if the same content was just added.
 */
export function addAssistantMessage(content: string, isFinal = true): void {
  ticketChat.addAssistantMessage(content, isFinal);
}

export function setTicketIndex(index: number | null): void {
  const changed = ticketChatState.ticketIndex !== index;
  ticketChatState.ticketIndex = index;
  ticketChatState.draft = null;
  resetTicketChatState();
  // Clear chat history when switching tickets
  if (changed) {
    ticketChat.setTarget(index != null ? String(index) : null);
  }
}

export function renderTicketChat(): void {
  const els = getTicketChatElements();

  // Shared chat render (status, events, messages)
  ticketChat.render();

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
        renderDiff(ticketChatState.draft!.patch || "(no changes)", els.patchBody);
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
        const [fmYaml, body] = splitMarkdownFrontmatter(res.content);
        if (fmYaml !== null) {
          textarea.value = body.trimStart();
        } else {
          textarea.value = res.content.trimStart();
        }
        // Trigger input event to update undo stack and autosave
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
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
