/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, flash } from "./utils.js";
import { performTicketChatRequest } from "./ticketChatStream.js";
import { publish } from "./bus.js";
import { saveTicketChatHistory, loadTicketChatHistory } from "./ticketChatStorage.js";
import { renderTicketMessages } from "./ticketChatEvents.js";
// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;
// Global state for ticket chat
export const ticketChatState = {
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
        input: document.getElementById("ticket-chat-input"),
        sendBtn: document.getElementById("ticket-chat-send"),
        voiceBtn: document.getElementById("ticket-chat-voice"),
        cancelBtn: document.getElementById("ticket-chat-cancel"),
        newThreadBtn: document.getElementById("ticket-chat-new-thread"),
        statusEl: document.getElementById("ticket-chat-status"),
        streamEl: document.getElementById("ticket-chat-stream"),
        // Content area elements - mutually exclusive with patch preview
        contentTextarea: document.getElementById("ticket-editor-content"),
        contentToolbar: document.getElementById("ticket-editor-toolbar"),
        // Patch preview elements - mutually exclusive with content area
        patchMain: document.getElementById("ticket-patch-main"),
        patchBody: document.getElementById("ticket-patch-body"),
        patchStatus: document.getElementById("ticket-patch-status"),
        applyBtn: document.getElementById("ticket-patch-apply"),
        discardBtn: document.getElementById("ticket-patch-discard"),
        agentSelect: document.getElementById("ticket-chat-agent-select"),
        modelSelect: document.getElementById("ticket-chat-model-select"),
        reasoningSelect: document.getElementById("ticket-chat-reasoning-select"),
        // Rich chat experience: events and messages
        eventsMain: document.getElementById("ticket-chat-events"),
        eventsList: document.getElementById("ticket-chat-events-list"),
        eventsCount: document.getElementById("ticket-chat-events-count"),
        eventsToggle: document.getElementById("ticket-chat-events-toggle"),
        messagesEl: document.getElementById("ticket-chat-messages"),
    };
}
export function resetTicketChatState() {
    ticketChatState.status = "idle";
    ticketChatState.error = "";
    ticketChatState.streamText = "";
    ticketChatState.statusText = "";
    ticketChatState.controller = null;
    // Note: events are cleared at the start of each new request, not here
    // Messages persist across requests within the same ticket
}
export async function startNewTicketChatThread() {
    if (ticketChatState.ticketIndex == null)
        return;
    const confirmed = window.confirm("Start a new conversation thread for this ticket?");
    if (!confirmed)
        return;
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
    }
    catch (err) {
        flash(`Failed to reset thread: ${err.message}`, "error");
    }
    finally {
        renderTicketChat();
        renderTicketMessages();
    }
}
/**
 * Clear events at the start of a new request.
 * Events are transient (thinking/tool calls) and reset each turn.
 */
export function clearTicketEvents() {
    ticketChatState.events = [];
    ticketChatState.eventItemIndex = {};
}
/**
 * Add a user message to the chat history.
 */
export function addUserMessage(content) {
    ticketChatState.messages.push({
        id: `user-${Date.now()}`,
        role: "user",
        content,
        time: new Date().toISOString(),
        isFinal: true,
    });
    if (ticketChatState.ticketIndex != null) {
        saveTicketChatHistory(ticketChatState.ticketIndex, ticketChatState.messages);
    }
}
/**
 * Add an assistant message to the chat history.
 * Prevents duplicates by checking if the same content was just added.
 */
export function addAssistantMessage(content, isFinal = true) {
    // Prevent duplicate messages with same content
    if (ticketChatState.messages.length > 0) {
        const lastMsg = ticketChatState.messages[ticketChatState.messages.length - 1];
        if (lastMsg.role === "assistant" && lastMsg.content === content) {
            return; // Skip duplicate
        }
    }
    ticketChatState.messages.push({
        id: `assistant-${Date.now()}`,
        role: "assistant",
        content,
        time: new Date().toISOString(),
        isFinal,
    });
    if (ticketChatState.ticketIndex != null) {
        saveTicketChatHistory(ticketChatState.ticketIndex, ticketChatState.messages);
    }
}
export function setTicketIndex(index) {
    const changed = ticketChatState.ticketIndex !== index;
    ticketChatState.ticketIndex = index;
    ticketChatState.draft = null;
    resetTicketChatState();
    // Clear chat history when switching tickets
    if (changed) {
        ticketChatState.messages = index != null ? loadTicketChatHistory(index) : [];
        clearTicketEvents();
    }
}
export function renderTicketChat() {
    const els = getTicketChatElements();
    // Update status pill
    if (els.statusEl) {
        const status = ticketChatState.status;
        let displayText = status;
        if (ticketChatState.error) {
            displayText = "error";
            els.statusEl.classList.add("error");
        }
        else {
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
    // Show/hide new thread button
    if (els.newThreadBtn) {
        const hasHistory = ticketChatState.messages.length > 0;
        els.newThreadBtn.classList.toggle("hidden", !hasHistory || ticketChatState.status === "running");
    }
    // The streamEl now contains events and messages sections.
    // Show the stream container when there are events, messages, or running.
    if (els.streamEl) {
        const hasContent = ticketChatState.events.length > 0 ||
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
                els.patchBody.textContent = ticketChatState.draft.patch || "(no changes)";
            }
            if (els.patchStatus) {
                els.patchStatus.textContent = ticketChatState.draft.agentMessage || "";
            }
        }
    }
}
export async function sendTicketChat() {
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
        await performTicketChatRequest(ticketChatState.ticketIndex, message, ticketChatState.controller.signal, {
            agent,
            model,
            reasoning,
        });
        // Try to load any pending draft
        await loadTicketPending(ticketChatState.ticketIndex, true);
        if (ticketChatState.status === "running") {
            ticketChatState.status = "done";
        }
    }
    catch (err) {
        const error = err;
        if (error.name === "AbortError") {
            ticketChatState.status = "interrupted";
            ticketChatState.error = "";
        }
        else {
            ticketChatState.status = "error";
            ticketChatState.error = error.message || "Ticket chat failed";
        }
    }
    finally {
        ticketChatState.controller = null;
        renderTicketChat();
    }
}
export async function cancelTicketChat() {
    if (ticketChatState.status !== "running")
        return;
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
        }
        catch (err) {
            // Ignore interrupt errors
        }
    }
    ticketChatState.status = "interrupted";
    ticketChatState.error = "";
    ticketChatState.statusText = "";
    ticketChatState.controller = null;
    renderTicketChat();
}
export async function applyTicketPatch() {
    if (ticketChatState.ticketIndex == null) {
        flash("No ticket selected", "error");
        return;
    }
    if (!ticketChatState.draft) {
        flash("No draft to apply", "error");
        return;
    }
    try {
        const res = await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/apply`, { method: "POST" });
        ticketChatState.draft = null;
        flash("Draft applied");
        // Notify that tickets changed
        publish("tickets:updated", {});
        // Update the editor textarea if content is returned
        if (res.content) {
            const textarea = document.getElementById("ticket-editor-content");
            if (textarea) {
                textarea.value = res.content;
            }
        }
    }
    catch (err) {
        const error = err;
        flash(error.message || "Failed to apply draft", "error");
    }
    finally {
        renderTicketChat();
    }
}
export async function discardTicketPatch() {
    if (ticketChatState.ticketIndex == null) {
        flash("No ticket selected", "error");
        return;
    }
    try {
        await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/discard`, { method: "POST" });
        ticketChatState.draft = null;
        flash("Draft discarded");
    }
    catch (err) {
        const error = err;
        flash(error.message || "Failed to discard draft", "error");
    }
    finally {
        renderTicketChat();
    }
}
export async function loadTicketPending(index, silent = false) {
    try {
        const res = await api(`/api/tickets/${index}/chat/pending`, { method: "GET" });
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
    }
    catch (err) {
        const error = err;
        const message = error?.message || "";
        if (message.includes("No pending")) {
            ticketChatState.draft = null;
            if (!silent) {
                flash("No pending draft");
            }
        }
        else if (!silent) {
            flash(message || "Failed to load pending draft", "error");
        }
    }
    finally {
        renderTicketChat();
    }
}
