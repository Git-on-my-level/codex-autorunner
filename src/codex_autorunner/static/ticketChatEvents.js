/**
 * Ticket Chat Events - handles parsing and rendering of agent events (thinking, tool calls, etc.)
 * Ported from docChatEvents.ts for the ticket chat experience.
 */
import { getTicketChatElements, ticketChatState, TICKET_CHAT_EVENT_LIMIT, TICKET_CHAT_EVENT_MAX, } from "./ticketChatActions.js";
import { parseAppServerEvent, } from "./agentEvents.js";
function addTicketEvent(state, entry) {
    state.events.push(entry);
    if (state.events.length > TICKET_CHAT_EVENT_MAX) {
        state.events = state.events.slice(-TICKET_CHAT_EVENT_MAX);
        state.eventItemIndex = {};
        state.events.forEach((evt, idx) => {
            if (evt.itemId)
                state.eventItemIndex[evt.itemId] = idx;
        });
    }
}
/**
 * Apply an App-server event to the ticket chat state.
 * This parses the event and adds it to the events array for display.
 */
export function applyTicketEvent(state, payload) {
    const parsed = parseAppServerEvent(payload);
    if (!parsed)
        return;
    const { event, mergeStrategy } = parsed;
    const itemId = event.itemId;
    if (mergeStrategy && itemId && state.eventItemIndex[itemId] !== undefined) {
        const existingIndex = state.eventItemIndex[itemId];
        const existing = state.events[existingIndex];
        if (mergeStrategy === "append") {
            existing.summary = `${existing.summary || ""}${event.summary}`;
        }
        else if (mergeStrategy === "newline") {
            existing.summary = `${existing.summary || ""}\n\n`;
        }
        existing.time = event.time;
        return;
    }
    const entry = {
        ...event,
    };
    addTicketEvent(state, entry);
    if (itemId)
        state.eventItemIndex[itemId] = state.events.length - 1;
}
/**
 * Render the ticket chat events list.
 * Shows agent activity (thinking, tool calls, etc.) during processing.
 */
export function renderTicketEvents() {
    const els = getTicketChatElements();
    if (!els.eventsMain || !els.eventsList || !els.eventsCount)
        return;
    const state = ticketChatState;
    const hasEvents = state.events.length > 0;
    const isRunning = state.status === "running";
    const showEvents = hasEvents || isRunning;
    els.eventsMain.classList.toggle("hidden", !showEvents);
    els.eventsCount.textContent = String(state.events.length);
    if (!showEvents)
        return;
    const limit = TICKET_CHAT_EVENT_LIMIT;
    const expanded = !!state.eventsExpanded;
    const showCount = expanded ? state.events.length : Math.min(state.events.length, limit);
    const visible = state.events.slice(-showCount);
    if (els.eventsToggle) {
        const hiddenCount = Math.max(0, state.events.length - showCount);
        els.eventsToggle.classList.toggle("hidden", hiddenCount === 0);
        els.eventsToggle.textContent = expanded ? "Show recent" : `Show more (${hiddenCount})`;
    }
    els.eventsList.innerHTML = "";
    if (!hasEvents) {
        if (isRunning) {
            const empty = document.createElement("div");
            empty.className = "ticket-chat-events-empty ticket-chat-events-waiting";
            empty.textContent = "Processing...";
            els.eventsList.appendChild(empty);
        }
        return;
    }
    visible.forEach((entry) => {
        const wrapper = document.createElement("div");
        wrapper.className = `ticket-chat-event ${entry.kind || ""}`.trim();
        const title = document.createElement("div");
        title.className = "ticket-chat-event-title";
        title.textContent = entry.title || entry.method || "Update";
        const summary = document.createElement("div");
        summary.className = "ticket-chat-event-summary";
        summary.textContent = entry.summary || "(no details)";
        wrapper.appendChild(title);
        wrapper.appendChild(summary);
        if (entry.detail) {
            const detail = document.createElement("div");
            detail.className = "ticket-chat-event-detail";
            detail.textContent = entry.detail;
            wrapper.appendChild(detail);
        }
        const meta = document.createElement("div");
        meta.className = "ticket-chat-event-meta";
        meta.textContent = entry.time
            ? new Date(entry.time).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
            })
            : "";
        wrapper.appendChild(meta);
        els.eventsList.appendChild(wrapper);
    });
    // Auto-scroll events list to bottom
    els.eventsList.scrollTop = els.eventsList.scrollHeight;
}
/**
 * Render the ticket chat messages history.
 * Shows user prompts and assistant responses with clear role labels.
 */
export function renderTicketMessages() {
    const els = getTicketChatElements();
    if (!els.messagesEl)
        return;
    const state = ticketChatState;
    els.messagesEl.innerHTML = "";
    if (state.messages.length === 0) {
        return;
    }
    state.messages.forEach((msg) => {
        const wrapper = document.createElement("div");
        const roleClass = msg.role === "user" ? "user" : "assistant";
        const finalClass = msg.isFinal ? "final" : "thinking";
        wrapper.className = `ticket-chat-message ${roleClass} ${finalClass}`.trim();
        // Add role label for clear differentiation
        const roleLabel = document.createElement("div");
        roleLabel.className = "ticket-chat-message-role";
        if (msg.role === "user") {
            roleLabel.textContent = "You";
        }
        else {
            // Show different label for thinking vs final response
            roleLabel.textContent = msg.isFinal ? "Response" : "Thinking";
        }
        wrapper.appendChild(roleLabel);
        const content = document.createElement("div");
        content.className = "ticket-chat-message-content";
        content.textContent = msg.content;
        wrapper.appendChild(content);
        const meta = document.createElement("div");
        meta.className = "ticket-chat-message-meta";
        const time = msg.time ? new Date(msg.time) : new Date();
        meta.textContent = time.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
        });
        wrapper.appendChild(meta);
        els.messagesEl.appendChild(wrapper);
    });
    // Auto-scroll messages to bottom
    els.messagesEl.scrollTop = els.messagesEl.scrollHeight;
}
/**
 * Initialize event handlers for the ticket chat events UI.
 */
export function initTicketChatEvents() {
    const els = getTicketChatElements();
    // Toggle events expansion
    if (els.eventsToggle) {
        els.eventsToggle.addEventListener("click", () => {
            ticketChatState.eventsExpanded = !ticketChatState.eventsExpanded;
            renderTicketEvents();
        });
    }
}
