// GENERATED FILE - do not edit directly. Source: static_src/
import { ticketChat } from "./ticketChatActions.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
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
