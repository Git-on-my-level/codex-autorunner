// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, confirmModal, flash, splitMarkdownFrontmatter } from "./utils.js";
import { performTicketChatRequest } from "./ticketChatStream.js";
import { renderTicketMessages, renderTicketEvents } from "./ticketChatEvents.js";
import { publish } from "./bus.js";
import { createDocChat } from "./docChatCore.js";
import { saveTicketChatHistory } from "./ticketChatStorage.js";
import { renderDiff } from "./diffRenderer.js";
import { newClientTurnId, streamTurnEvents } from "./fileChat.js";
import { loadPendingTurn, savePendingTurn, clearPendingTurn } from "./turnResume.js";
import { resumeFileChatTurn } from "./turnEvents.js";
// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;
const pendingKeyForTicket = (index) => index != null ? `car.ticketChat.pending.${index}` : "car.ticketChat.pending";
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
export const ticketChatState = Object.assign(ticketChat.state, {
    ticketIndex: null,
    draft: null,
    contextUsagePercent: null,
});
let currentTurnEventsController = null;
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
    };
}
export function resetTicketChatState() {
    ticketChatState.status = "idle";
    ticketChatState.error = "";
    ticketChatState.streamText = "";
    ticketChatState.statusText = "";
    ticketChatState.controller = null;
    ticketChatState.contextUsagePercent = null;
    // Note: events are cleared at the start of each new request, not here
    // Messages persist across requests within the same ticket
}
export async function startNewTicketChatThread() {
    if (ticketChatState.ticketIndex == null)
        return;
    const confirmed = await confirmModal("Start a new conversation thread for this ticket?");
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
    ticketChat.clearEvents();
}
function clearTurnEventsStream() {
    if (currentTurnEventsController) {
        try {
            currentTurnEventsController.abort();
        }
        catch {
            // ignore
        }
        currentTurnEventsController = null;
    }
}
function clearPendingTurnState(pendingKey) {
    clearTurnEventsStream();
    clearPendingTurn(pendingKey);
}
function handleTicketTurnMeta(update) {
    const threadId = typeof update.thread_id === "string" ? update.thread_id : "";
    const turnId = typeof update.turn_id === "string" ? update.turn_id : "";
    const agent = typeof update.agent === "string" ? update.agent : "codex";
    if (!threadId || !turnId)
        return;
    clearTurnEventsStream();
    currentTurnEventsController = streamTurnEvents({ agent, threadId, turnId }, {
        onEvent: (event) => {
            ticketChat.applyAppEvent(event);
            ticketChat.renderEvents();
            ticketChat.render();
        },
    });
}
export function applyTicketChatResult(payload) {
    if (!payload || typeof payload !== "object")
        return;
    const result = payload;
    handleTicketTurnMeta(result);
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
            result.detail || result.error || "Chat failed";
        addAssistantMessage(`Error: ${ticketChatState.error}`, true);
        renderTicketChat();
        renderTicketMessages();
        return;
    }
    // Success
    ticketChatState.status = "done";
    if (result.message) {
        ticketChatState.streamText = result.message;
    }
    if (result.agent_message || result.agentMessage) {
        ticketChatState.statusText =
            result.agent_message || result.agentMessage || "";
    }
    // Check for draft/patch in response
    const hasDraft = result.has_draft ?? result.hasDraft;
    if (hasDraft === false) {
        ticketChatState.draft = null;
    }
    else if (hasDraft === true || result.draft || result.patch || result.content) {
        ticketChatState.draft = {
            content: result.content || "",
            patch: result.patch || "",
            agentMessage: result.agent_message || result.agentMessage || "",
            createdAt: result.created_at || result.createdAt || "",
            baseHash: result.base_hash || result.baseHash || "",
        };
    }
    // Add assistant message from response
    const responseText = ticketChatState.streamText ||
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
/**
 * Add a user message to the chat history.
 */
export function addUserMessage(content) {
    ticketChat.addUserMessage(content);
}
/**
 * Add an assistant message to the chat history.
 * Prevents duplicates by checking if the same content was just added.
 */
export function addAssistantMessage(content, isFinal = true) {
    ticketChat.addAssistantMessage(content, isFinal);
}
export function setTicketIndex(index) {
    const changed = ticketChatState.ticketIndex !== index;
    ticketChatState.ticketIndex = index;
    ticketChatState.draft = null;
    resetTicketChatState();
    clearTurnEventsStream();
    // Clear chat history when switching tickets
    if (changed) {
        ticketChat.setTarget(index != null ? String(index) : null);
    }
}
export function renderTicketChat() {
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
                renderDiff(ticketChatState.draft.patch || "(no changes)", els.patchBody);
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
    clearTurnEventsStream();
    ticketChatState.controller = new AbortController();
    const pendingKey = pendingKeyForTicket(ticketChatState.ticketIndex);
    const clientTurnId = newClientTurnId("ticket");
    savePendingTurn(pendingKey, {
        clientTurnId,
        message,
        startedAtMs: Date.now(),
        target: ticketChatState.ticketIndex != null ? `ticket:${ticketChatState.ticketIndex}` : "ticket",
    });
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
            clientTurnId,
        });
        // Try to load any pending draft
        await loadTicketPending(ticketChatState.ticketIndex, true);
        if (ticketChatState.status === "running") {
            ticketChatState.status = "done";
        }
        clearPendingTurnState(pendingKey);
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
        clearPendingTurnState(pendingKey);
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
    clearTurnEventsStream();
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
    if (ticketChatState.ticketIndex != null) {
        clearPendingTurnState(pendingKeyForTicket(ticketChatState.ticketIndex));
    }
}
export async function resumeTicketPendingTurn(index) {
    if (index == null)
        return;
    const pendingKey = pendingKeyForTicket(index);
    const pending = loadPendingTurn(pendingKey);
    if (!pending || pending.target !== `ticket:${index}`)
        return;
    const chatState = ticketChatState;
    chatState.status = "running";
    chatState.statusText = "Recovering previous turn…";
    ticketChat.render();
    ticketChat.renderMessages();
    try {
        const outcome = await resumeFileChatTurn(pending.clientTurnId, {
            onEvent: (event) => {
                ticketChat.applyAppEvent(event);
                ticketChat.renderEvents();
                ticketChat.render();
            },
            onResult: (result) => {
                applyTicketChatResult(result);
                const status = result.status;
                if (status === "ok" || status === "error" || status === "interrupted") {
                    clearPendingTurnState(pendingKey);
                }
            },
            onError: (msg) => {
                chatState.statusText = msg;
                renderTicketChat();
            },
        });
        currentTurnEventsController = outcome.controller;
        if (outcome.lastResult && outcome.lastResult.status) {
            applyTicketChatResult(outcome.lastResult);
            clearPendingTurnState(pendingKey);
            return;
        }
        if (!outcome.controller) {
            window.setTimeout(() => void resumeTicketPendingTurn(index), 1000);
        }
    }
    catch (err) {
        const msg = err.message || "Failed to resume turn";
        chatState.statusText = msg;
        renderTicketChat();
    }
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
                const [fmYaml, body] = splitMarkdownFrontmatter(res.content);
                if (fmYaml !== null) {
                    textarea.value = body.trimStart();
                }
                else {
                    textarea.value = res.content.trimStart();
                }
                // Trigger input event to update undo stack and autosave
                textarea.dispatchEvent(new Event("input", { bubbles: true }));
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
