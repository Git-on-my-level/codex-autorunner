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

export interface TicketChatState {
  status: TicketChatStatus;
  ticketIndex: number | null;
  error: string;
  streamText: string;
  statusText: string;
  controller: AbortController | null;
  draft: TicketDraft | null;
}

// Global state for ticket chat
export const ticketChatState: TicketChatState = {
  status: "idle",
  ticketIndex: null,
  error: "",
  streamText: "",
  statusText: "",
  controller: null,
  draft: null,
};

function getTicketChatElements() {
  return {
    input: document.getElementById("ticket-chat-input") as HTMLTextAreaElement | null,
    sendBtn: document.getElementById("ticket-chat-send") as HTMLButtonElement | null,
    voiceBtn: document.getElementById("ticket-chat-voice") as HTMLButtonElement | null,
    cancelBtn: document.getElementById("ticket-chat-cancel") as HTMLButtonElement | null,
    statusEl: document.getElementById("ticket-chat-status") as HTMLElement | null,
    streamEl: document.getElementById("ticket-chat-stream") as HTMLElement | null,
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
}

export function setTicketIndex(index: number | null): void {
  ticketChatState.ticketIndex = index;
  ticketChatState.draft = null;
  resetTicketChatState();
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

  // Show stream text
  if (els.streamEl) {
    if (ticketChatState.streamText) {
      els.streamEl.textContent = ticketChatState.streamText;
      els.streamEl.classList.remove("hidden");
    } else {
      els.streamEl.classList.add("hidden");
    }
  }

  // Show/hide patch preview
  if (els.patchMain) {
    if (ticketChatState.draft) {
      els.patchMain.classList.remove("hidden");
      if (els.patchBody) {
        els.patchBody.textContent = ticketChatState.draft.patch || "(no changes)";
      }
      if (els.patchStatus) {
        els.patchStatus.textContent = ticketChatState.draft.agentMessage || "";
      }
    } else {
      els.patchMain.classList.add("hidden");
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
