// GENERATED FILE - do not edit directly. Source: static_src/
import { ticketChat } from "./ticketChatActions.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
// This module now delegates to docChatCore for rendering and event parsing.
export function applyTicketEvent(payload) {
    ticketChat.applyAppEvent(payload);
}
export function renderTicketEvents() {
    ticketChat.renderEvents();
}
export function renderTicketMessages() {
    ticketChat.renderMessages();
}
export function initTicketChatEvents() {
    // Toggle already wired in docChatCore constructor.
    return;
}
