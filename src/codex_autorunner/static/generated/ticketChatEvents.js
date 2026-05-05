// GENERATED FILE - do not edit directly. Source: static_src/
import { ticketChat } from "./ticketChatActions.js?v=be399e9b80baaceac895399d521c7e33ba6116a6282d86fe16aaac8dd380e544";
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
