// GENERATED FILE - do not edit directly. Source: static_src/
import { ticketChat } from "./ticketChatActions.js?v=510fd0419ed9eddfa5851d4093853609591d2a4765ecd74f3add9600783da27f";
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
